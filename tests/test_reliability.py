import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile
from contextlib import closing

import reliability as r
import state_store as st
from locking import instance_lock


class FakeRelease:
    """Models immutable GitHub assets and injected transport/data failures."""
    def __init__(self):
        self.files = {}
        self.fail_upload = False
        self.corrupt_download = set()

    def assets(self):
        return [dict(name=name, size=len(data)) for name, data in self.files.items()]

    def upload(self, path):
        path = Path(path)
        if self.fail_upload:
            raise st.StateError('simulated upload failure')
        if path.name in self.files:
            raise st.StateError('asset already exists')
        self.files[path.name] = path.read_bytes()

    def download(self, name, directory):
        if name not in self.files:
            raise st.StateError('missing asset')
        path = Path(directory) / name
        path.write_bytes(b'corrupt' if name in self.corrupt_download else self.files[name])
        return path


def response(code=200, data=None):
    return Mock(status_code=code, json=Mock(return_value=data), text='mock')


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_temp = tempfile.TemporaryDirectory()
        os.environ['DB_FILE'] = str(Path(cls.import_temp.name) / 'import.db')
        os.environ['SNIPER_ALLOW_NEW_DB'] = '1'
        os.environ['TG_TOKEN'] = ''
        os.environ['TG_CHAT_ID'] = ''
        cls.bot = importlib.import_module('sniper')
        cls.bot.close_db()

    @classmethod
    def tearDownClass(cls):
        cls.bot.close_db()
        cls.import_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'sniper.db'
        self.settings = self.root / 'settings.json'
        self.settings.write_text(json.dumps(dict(max_price=750)), encoding='utf-8')
        self.bot.DB_FILE = str(self.db)
        self.bot.SETTINGS_FILE = str(self.settings)
        self.bot.RUNNING = True
        self.bot.db_conn = self.bot.init_db()
        self.conn = self.bot.db_conn
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected real network request'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def tearDown(self):
        self.bot.close_db()
        self.temp.cleanup()

    def seed(self, price=300, ad_id='123', status='active', notified=0):
        now = int(time.time())
        with self.conn:
            self.conn.execute("""INSERT INTO lot_history
                (ad_id,model,memory,first_seen_price,last_seen_price,first_seen_ts,last_seen_ts,last_active_ts,list_time_ts,status,notified,title)
                VALUES (?,'iPhone 12',128,?,?,?, ?,?,?,?,?,'iPhone 12 128GB')""",
                (ad_id, price, price, now-3600, now-900, now-900, now-3600, status, notified))
            self.conn.execute("INSERT INTO ml_ads(ad_id,price_byn,status,first_seen_ts,last_seen_ts) VALUES (?,?,?,?,?)", (ad_id, price, status, now-3600, now-900))
            self.bot.record_price_event(self.conn.cursor(), ad_id, price, now-3600)

    def ad(self, ad_id='123', price=300):
        return dict(ad_id=ad_id, account_id='owner', subject='iPhone 12 128GB', price_byn=str(round(price*100)),
                    list_time='2026-11-26T12:00:00Z', ad_parameters=[], images=[], ad_link='https://www.kufar.by/item/' + ad_id)

    def market(self):
        return dict(price=450, showcase_price=450, sold_median=None, sold_count=0,
                    fast_sold_count=0, sold_weight_pct=0, sample_size=10, median_days=5,
                    min_price=400, min_link='', is_correlated=False, is_anchored=False)

    def process(self, ad):
        with patch.object(self.bot, 'get_ad_details', return_value=('Телефон работает. Без дефектов.', {'is_active':True}, True)):
            return self.bot.process_ad(ad, self.bot.DEFAULT_SETTINGS.copy(), 0)

    def snapshot(self, baseline=None):
        return st.make_snapshot(self.db, self.settings, self.root / 'snapshots', baseline)

    def test_market_failure_does_not_lose_unchanged_ad(self):
        ad = self.ad()
        with self.conn:
            r.remember_ads(self.conn, [ad])
        with patch.object(self.bot, 'get_market_data', return_value=None):
            with self.assertRaises(r.RetryAd):
                self.process(ad)
        r.finish_ad(self.conn, '123', 'market_missing', now=100)
        self.assertEqual(len(r.pending_ads(self.conn, now=200)), 1)
        with patch.object(self.bot, 'get_market_data', return_value=self.market()):
            self.process(r.pending_ads(self.conn, now=200)[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM price_history').fetchone()[0], 1)

    def test_stale_feed_does_not_change_price_when_details_fail(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'current card')
        with patch.object(self.bot, 'get_ad_details', return_value=('', {}, False)):
            with self.assertRaises(r.RetryAd):
                self.bot.process_ad(self.ad(price=320), self.bot.DEFAULT_SETTINGS.copy(), 0)
        self.assertEqual(self.conn.execute('SELECT last_seen_price FROM lot_history').fetchone()[0], 300)
        self.assertEqual(self.conn.execute('SELECT price_byn FROM ml_ads').fetchone()[0], 300)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM price_history').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0], 'pending')

    def test_current_detail_price_wins_without_false_history_or_duplicate(self):
        self.seed()
        with self.conn:
            r.remember_ads(self.conn, [self.ad(price=320)])
            r.queue_notification(self.conn, '123', 300, 'current card')
        with patch.object(self.bot, 'get_ad_details', return_value=('Рабочий телефон', {'is_active': True, 'price_byn': '30000'}, True)), \
             patch.object(self.bot, 'get_market_data', return_value=self.market()):
            self.bot.process_ad(self.ad(price=320), self.bot.DEFAULT_SETTINGS.copy(), 0)
        self.assertEqual(self.conn.execute('SELECT last_seen_price FROM lot_history').fetchone()[0], 300)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM price_history').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT price FROM notification_outbox').fetchone()[0], 300)
        self.assertEqual(json.loads(self.conn.execute('SELECT payload FROM feed_inbox').fetchone()[0])['price_byn'], '30000')

    def test_processing_404_records_one_observation(self):
        self.seed()
        with patch.object(self.bot.kufar_session, 'get', return_value=response(code=404)):
            self.bot.process_ad(self.ad(price=320), self.bot.DEFAULT_SETTINGS.copy(), 0)
        rows = self.conn.execute('SELECT evidence FROM availability_history').fetchall()
        self.assertEqual([row[0] for row in rows], ['http_404'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM price_history').fetchone()[0], 1)

    def test_current_title_is_used_for_model_and_memory(self):
        with patch.object(self.bot, 'get_ad_details', return_value=('Рабочий телефон',
                {'is_active': True, 'subject': 'iPhone 13 256GB', 'price_byn': '30000'}, True)), \
             patch.object(self.bot, 'get_market_data', return_value=self.market()):
            self.bot.process_ad(self.ad(), self.bot.DEFAULT_SETTINGS.copy(), 0)
        row = self.conn.execute('SELECT model,memory,title FROM lot_history').fetchone()
        self.assertEqual(tuple(row), ('iPhone 13', 256, 'iPhone 13 256GB'))
        self.assertEqual(self.conn.execute('SELECT subject FROM ml_ads').fetchone()[0], 'iPhone 13 256GB')

    def test_outside_notification_budget_saves_data_without_market_requests(self):
        with patch.object(self.bot, 'get_market_data') as market:
            self.process(self.ad(price=1000))
        market.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT price_byn FROM ml_ads').fetchone()[0], 1000)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM price_history').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)

    def test_due_retries_share_batch_with_newest_listings(self):
        retry = self.ad(ad_id='retry')
        fresh = self.ad(ad_id='fresh')
        old = [dict(self.ad(ad_id=str(i)), list_time='2026-10-01T10:00:00Z') for i in range(30)]
        with self.conn:
            r.remember_ads(self.conn, [retry, fresh], now=1_800_000_000)
            r.remember_ads(self.conn, old, now=1_800_000_001)
        r.finish_ad(self.conn, 'retry', 'temporary', now=1_800_000_001)
        ids = [ad['ad_id'] for ad in r.pending_ads(self.conn, limit=10, now=1_800_000_100)]
        self.assertIn('retry', ids[:2])
        self.assertIn('fresh', ids[:2])
        self.assertEqual(len(ids), len(set(ids)))

    def test_failed_send_survives_restart_then_sends_once(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'card')
        sender = Mock(side_effect=[dict(ok=False, error='http_500'), dict(ok=True, message_id=42)])
        r.drain_outbox(self.conn, sender, now=1000)
        self.bot.close_db()
        self.bot.db_conn = self.bot.init_db()
        self.conn = self.bot.db_conn
        r.drain_outbox(self.conn, sender, now=1031)
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'same card')
        r.drain_outbox(self.conn, sender, now=2000)
        self.assertEqual(sender.call_count, 2)
        row = self.conn.execute('SELECT state,message_id FROM notification_outbox').fetchone()
        self.assertEqual(tuple(row), ('sent', 42))
        self.assertEqual(self.conn.execute('SELECT notified FROM lot_history').fetchone()[0], 1)

    def test_pending_card_refresh_keeps_retry_delay_and_sent_card_is_immutable(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'old description')
        sender = Mock(return_value=dict(ok=False, error='http_429', retry_after=120))
        r.drain_outbox(self.conn, sender, now=1000)
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'current description')
        row = self.conn.execute('SELECT * FROM notification_outbox').fetchone()
        self.assertEqual(json.loads(row['payload'])['text'], 'current description')
        self.assertEqual((row['attempts'], row['next_attempt_ts']), (1, 1120))
        r.drain_outbox(self.conn, sender, now=1119)
        self.assertEqual(sender.call_count, 1)
        sender.return_value = dict(ok=True, message_id=42)
        r.drain_outbox(self.conn, sender, now=1120)
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'third description')
        row = self.conn.execute('SELECT state,message_id,payload FROM notification_outbox').fetchone()
        self.assertEqual(json.loads(row['payload'])['text'], 'current description')
        self.assertEqual(tuple(row)[:2], ('sent', 42))
        self.assertEqual(self.conn.execute('SELECT notified FROM lot_history').fetchone()[0], 1)

    def test_retry_after_is_honored(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'card')
        sender = Mock(return_value=dict(ok=False, error='rate_limit', retry_after=120))
        r.drain_outbox(self.conn, sender, now=100)
        r.drain_outbox(self.conn, sender, now=219)
        self.assertEqual(sender.call_count, 1)
        r.drain_outbox(self.conn, sender, now=220)
        self.assertEqual(sender.call_count, 2)

    def test_crash_during_send_is_explicitly_recovered(self):
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'card')
            self.conn.execute("UPDATE notification_outbox SET state='sending'")
        r.recover_outbox(self.conn)
        row = self.conn.execute('SELECT state,last_error FROM notification_outbox').fetchone()
        self.assertEqual(tuple(row), ('pending', 'delivery_unknown_after_restart'))

    def test_repeated_evaluation_no_duplicate_and_drop_keeps_history(self):
        with patch.object(self.bot, 'get_market_data', return_value=self.market()):
            self.process(self.ad(price=320))
            self.process(self.ad(price=320))
            r.drain_outbox(self.conn, Mock(return_value=dict(ok=True)), now=int(time.time())+1)
            self.process(self.ad(price=300))
            self.process(self.ad(price=300))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM lot_history').fetchone()[0], 1)
        self.assertEqual([row[0] for row in self.conn.execute('SELECT price_byn FROM price_history ORDER BY id')], [320,300])
        self.assertEqual(self.conn.execute('SELECT first_seen_price,price_drops_count FROM lot_history').fetchone()[0], 320)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 2)

    def test_send_photo_timeout_does_not_fallback_to_second_message(self):
        with patch.object(self.bot, 'TG_TOKEN', 'test-not-a-secret'), patch.object(self.bot, 'TG_CHAT_ID', '1'), \
             patch.object(self.bot.tg_session, 'post', side_effect=TimeoutError) as post:
            result = self.bot.send_notification(dict(text='card', photo_url='https://example.test/a.jpg'))
        self.assertFalse(result['ok'])
        self.assertEqual(post.call_count, 1)

    def test_unavailable_is_not_a_sale_and_cancels_pending(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'card')
            r.mark_unavailable(self.conn, '123', int(time.time()), 'http_404')
        row = self.conn.execute('SELECT status,estimated_sold_price,confirmed_sale_price,last_active_ts,last_seen_ts FROM lot_history').fetchone()
        self.assertEqual(row['status'], 'unavailable')
        self.assertIsNone(row['estimated_sold_price'])
        self.assertIsNone(row['confirmed_sale_price'])
        self.assertEqual(row['last_active_ts'], row['last_seen_ts'])
        self.assertEqual(self.bot.get_sold_market_stats('iPhone 12',128), (None,0,0))
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0], 'cancelled')

    def test_legacy_schema_migration_is_lossless_and_idempotent(self):
        self.bot.close_db()
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as old, old:
            old.executescript(Path(__file__).with_name('fixtures').joinpath('legacy-schema.sql').read_text(encoding='utf-8'))
            old.execute("INSERT INTO lot_history(ad_id,model,memory,status,estimated_sold_price,last_seen_price,last_seen_ts,notified) VALUES ('old','iPhone 12',128,'sold',270,300,1000,1)")
            old.execute("INSERT INTO ml_ads(ad_id,status,sold_ts,estimated_sold_price) VALUES ('old','sold',900,270)")
            old.execute("INSERT INTO price_history(ad_id,price_byn,ts) VALUES ('old',350,500)")
            old.execute("INSERT INTO lot_history(ad_id,model,memory,status,last_seen_price,last_seen_ts,notified,title) VALUES ('lost','iPhone 12',128,'active',300,700,0,'iPhone 12 128GB')")
            old.execute("INSERT INTO ml_ads(ad_id,subject,price_byn,status,last_seen_ts) VALUES ('lost','iPhone 12 128GB',300,'active',700)")
        original = self.root / 'original.db'
        shutil.copyfile(self.db, original)
        self.bot.db_conn = self.bot.init_db()
        self.conn = self.bot.db_conn
        r.migrate(self.conn)
        row = self.conn.execute('SELECT * FROM lot_history').fetchone()
        self.assertEqual((row['status'], row['legacy_status'], row['legacy_estimated_sold_price']), ('unavailable','sold',270))
        self.assertIsNone(row['estimated_sold_price'])
        self.assertEqual(self.conn.execute('SELECT legacy_sold_ts,sold_ts FROM ml_ads').fetchone()[0], 900)
        st.compare_history(original, self.db)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feed_inbox').fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT last_active_ts FROM lot_history WHERE ad_id='lost'").fetchone()[0],700)

    def test_pagination_collects_more_than_50_and_deduplicates_overlap(self):
        first = [self.ad(str(i)) for i in range(50)]
        second = [self.ad(str(i)) for i in range(45,95)]
        with patch.object(self.bot.kufar_session,'get',side_effect=[
            response(data=dict(ads=first,pagination=dict(pages=[dict(label='next',token='opaque +/&')]))),
            response(data=dict(ads=second,pagination=dict(pages=[])))]) as get, patch.object(self.bot.time,'sleep'):
            ads = self.bot.get_latest_ads(self.bot.DEFAULT_SETTINGS.copy())
        self.assertEqual(len(ads),95)
        self.assertIn('opaque%20%2B%2F%26', get.call_args_list[1].args[0])
        self.assertIn('550000', get.call_args_list[0].args[0])

    def test_pagination_failure_preserves_cursor_and_pending_ads(self):
        with patch.object(self.bot.kufar_session,'get',side_effect=[
            response(data=dict(ads=[self.ad()],pagination=dict(pages=[dict(label='next',token='resume')]))), response(code=429)]), patch.object(self.bot.time,'sleep'):
            self.bot.get_latest_ads(self.bot.DEFAULT_SETTINGS.copy())
        self.assertEqual(self.bot.get_meta('feed_backfill_cursor'), 'resume')
        self.bot.set_meta('feed_retry_until','0')
        with patch.object(self.bot.kufar_session,'get',side_effect=[
            response(data=dict(ads=[self.ad()],pagination=dict(pages=[dict(label='next',token='head')]))),
            response(data=dict(ads=[self.ad('999')],pagination=dict(pages=[])))]) as get, patch.object(self.bot.time,'sleep'):
            self.bot.get_latest_ads(self.bot.DEFAULT_SETTINGS.copy())
        self.assertIn('cursor=resume', get.call_args_list[1].args[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feed_inbox').fetchone()[0],2)

    def test_invalid_feed_does_not_advance_cursor(self):
        self.bot.set_meta('feed_backfill_cursor','keep')
        with patch.object(self.bot.kufar_session,'get',return_value=response(data={})), patch.object(self.bot.time,'sleep'):
            self.bot.get_latest_ads(self.bot.DEFAULT_SETTINGS.copy())
        self.assertEqual(self.bot.get_meta('feed_backfill_cursor'),'keep')

    def test_snapshot_roundtrip_includes_settings_and_queues(self):
        self.seed()
        with self.conn:
            r.remember_ads(self.conn,[self.ad()])
            r.queue_notification(self.conn,'123',300,'card')
        archive = self.snapshot()
        restored = self.root / 'restored.db'
        settings = self.root / 'restored-settings.json'
        st.install_snapshot(archive, restored, settings)
        st.compare_history(self.db, restored)
        self.assertEqual(st.describe_db(restored),st.describe_db(self.db))
        self.assertEqual(settings.read_bytes(),self.settings.read_bytes())

    def test_upload_failure_retains_previous_version(self):
        self.seed()
        store = FakeRelease()
        first = self.snapshot()
        st.publish(store, first)
        previous = dict(store.files)
        store.fail_upload = True
        with self.assertRaises(st.StateError):
            st.publish(store, self.snapshot())
        self.assertEqual(store.files, previous)
        st.restore_release(store,self.root/'restored.db',self.root/'restored.json',self.root/'baseline.db')

    def test_failed_readback_is_not_committed(self):
        self.seed()
        archive = self.snapshot()
        store = FakeRelease()
        store.corrupt_download.add(archive.name)
        with self.assertRaises(st.StateError):
            st.publish(store,archive)
        self.assertFalse(any(name.endswith('.ready.json') for name in store.files))
        with self.assertRaises(st.StateError):
            st.restore_release(store,self.root/'restore.db',self.root/'s.json',self.root/'base.db')
        self.assertFalse((self.root/'restore.db').exists())

    def test_restore_falls_back_to_previous_committed_snapshot(self):
        self.seed()
        store = FakeRelease()
        first = self.snapshot()
        st.publish(store,first)
        # Deterministically order the next immutable snapshot after the first.
        second = self.snapshot()
        second_named = second.with_name('sniper-state-99999999T000000Z-test.zip')
        second.rename(second_named)
        st.publish(store,second_named)
        store.corrupt_download.add(second_named.name)
        restored_name = st.restore_release(store,self.root/'restored.db',self.root/'restored.json',self.root/'baseline.db')
        self.assertEqual(restored_name,first.name)
        st.compare_history(self.db,self.root/'restored.db')

    def test_missing_release_state_is_not_first_run(self):
        for files in ({}, {'settings.json':b'{}'}, {'sniper-state-uncommitted.zip':b'bad'}):
            store = FakeRelease()
            store.files = files
            with self.assertRaises(st.StateError):
                st.restore_release(store,self.root/'missing.db',self.root/'missing.json',self.root/'base.db')
            self.assertFalse((self.root/'missing.db').exists())

    def test_bad_restore_does_not_touch_existing_database(self):
        self.seed()
        before = st.sha256(self.db)
        bad = self.root / 'bad.zip'
        bad.write_bytes(b'broken')
        with self.assertRaises(zipfile.BadZipFile):
            st.install_snapshot(bad,self.db,self.settings,overwrite=True)
        self.assertEqual(st.sha256(self.db),before)

    def test_history_deletion_blocks_snapshot_even_if_file_grows(self):
        self.seed()
        baseline = self.root / 'baseline.db'
        with st.readonly(self.db) as source, closing(sqlite3.connect(baseline)) as target:
            source.backup(target)
        with self.conn:
            self.conn.execute('DELETE FROM price_history')
            self.conn.execute("INSERT INTO price_history(ad_id,price_byn,ts) VALUES ('other',500,999)")
        with self.assertRaises(st.StateError):
            self.snapshot(baseline)

    def test_runner_refuses_missing_db_and_no_secrets_printed(self):
        env = dict(os.environ,DB_FILE=str(self.root/'nonexistent.db'))
        env.pop('SNIPER_ALLOW_NEW_DB',None)
        result = subprocess.run([sys.executable,'runner.py','--init-only'],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'nonexistent.db').exists())

    def test_report_uses_observations_not_confirmed_sales(self):
        self.seed()
        with self.conn:
            r.mark_unavailable(self.conn,'123',int(time.time()),'http_404')
        path,stats = self.bot.generate_html_db_report(self.bot.DEFAULT_SETTINGS.copy())
        report = Path(path).read_text(encoding='utf-8')
        self.assertIn('Исчезновение не подтверждает продажу',report)
        self.assertNotIn('Реальные продажи',report)
        self.assertEqual(stats['unavailable'],1)

    def test_report_escapes_listing_text_and_keeps_links_on_kufar(self):
        ad_id = '../"><img src=x onerror=alert(1)>'
        self.seed(ad_id=ad_id)
        with self.conn:
            self.conn.execute('UPDATE lot_history SET title=?,description=?,district=? WHERE ad_id=?',
                              ('<script>alert(1)</script>', 'Камера <img onerror=alert(2)> $total', '<b>район</b>', ad_id))
        path, _ = self.bot.generate_html_db_report(self.bot.DEFAULT_SETTINGS.copy())
        report = Path(path).read_text(encoding='utf-8')
        self.assertEqual(report.count('<script>'), 1)  # only the report's search script
        self.assertNotIn('<img', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn('$total', report)  # user text cannot substitute template values
        self.assertIn('https://www.kufar.by/item/..%2F%22%3E%3Cimg', report)

    def test_empty_report_and_zero_day_sample_are_displayed(self):
        path, stats = self.bot.generate_html_db_report(self.bot.DEFAULT_SETTINGS.copy())
        self.assertEqual(stats['total'], 0)
        self.assertEqual(Path(path).read_text(encoding='utf-8').count('Пока нет данных'), 3)
        self.seed()
        with self.conn:
            self.conn.execute('UPDATE lot_history SET list_time_ts=last_active_ts')
            self.conn.execute("INSERT INTO market_cache(cache_key,market_price,sample_size,median_days) VALUES ('iPhone 12_128GB',450,10,0)")
        path, _ = self.bot.generate_html_db_report(self.bot.DEFAULT_SETTINGS.copy())
        report = Path(path).read_text(encoding='utf-8')
        self.assertGreaterEqual(report.count('0.0 дн.'), 2)
        self.assertIn('background: #0f172a', report)

    def test_outbox_preflight_404_does_not_send_or_double_count(self):
        self.seed()
        with self.conn:
            r.queue_notification(self.conn, '123', 300, 'card')
        with patch.object(self.bot, 'TG_TOKEN', 'not-real'), patch.object(self.bot, 'TG_CHAT_ID', '1'), \
             patch.object(self.bot.kufar_session, 'get', return_value=response(code=404)), \
             patch.object(self.bot.tg_session, 'post') as post:
            r.drain_outbox(self.conn, self.bot.send_notification, now=int(time.time()) + 1)
        post.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM availability_history').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT evidence FROM availability_history').fetchone()[0], 'http_404')
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0], 'cancelled')

    def test_db_menu_works_only_for_authorized_chat(self):
        for command in ('/db', '📦 База / Стата'):
            updates = [{'update_id': i, 'message': {'chat': {'id': chat}, 'text': command}}
                       for i, chat in enumerate((99, 1), 1)]
            with patch.object(self.bot, 'TG_TOKEN', 'not-real'), patch.object(self.bot, 'TG_CHAT_ID', '1'), \
                 patch.object(self.bot.tg_session, 'get', return_value=response(data={'result': updates})), \
                 patch.object(self.bot, 'send_db_report') as report:
                self.bot.handle_telegram_updates(0, self.bot.DEFAULT_SETTINGS.copy(), force=True)
            report.assert_called_once()

    def test_failed_shift_blocks_stale_state_until_explicit_recovery(self):
        self.seed()
        store = FakeRelease()
        st.publish(store,self.snapshot())
        st.begin_run(store,'12345-1')
        with self.assertRaises(st.StateError):
            st.restore_release(store,self.root/'restored.db',self.root/'restored.json',self.root/'baseline.db')
        archive = self.snapshot()
        st.publish(store,archive)
        st.finish_run(store,'12345-1',archive.name)
        st.restore_release(store,self.root/'restored.db',self.root/'restored.json',self.root/'baseline.db')
        st.compare_history(self.db,self.root/'restored.db')

    def test_restore_refuses_while_runner_holds_database_lock(self):
        self.seed()
        archive = self.snapshot()
        before = st.sha256(self.db)
        with instance_lock(str(self.db)+'.lock'):
            with self.assertRaises(OSError):
                st.install_snapshot(archive,self.db,self.settings,overwrite=True)
        self.assertEqual(st.sha256(self.db),before)

    def test_singleton_lock_released_on_close(self):
        lockpath = self.root / 'one.lock'
        with instance_lock(lockpath):
            with self.assertRaises(OSError):
                instance_lock(lockpath)
        with instance_lock(lockpath):
            pass

    def test_invalid_settings_do_not_silently_reset_budget(self):
        self.settings.write_text('{broken',encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.bot.load_settings()

    def test_preflight_price_changed_does_not_send_old_card(self):
        self.seed()
        with self.conn:
            r.remember_ads(self.conn,[self.ad()])
            r.queue_notification(self.conn,'123',300,'card')
        with patch.object(self.bot,'TG_TOKEN','not-real'), patch.object(self.bot,'TG_CHAT_ID','1'), \
             patch.object(self.bot,'get_ad_details',return_value=('',dict(price_byn='40000'),True)), \
             patch.object(self.bot.tg_session,'post') as post:
            r.drain_outbox(self.conn,self.bot.send_notification,now=int(time.time())+1)
        post.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0],'cancelled')
        self.assertEqual(r.pending_ads(self.conn)[0]['price_byn'],'40000')

    def test_revisions_keep_original_description_and_asking_prices(self):
        with self.conn:
            r.remember_ads(self.conn,[self.ad(price=350),self.ad(price=300),self.ad(price=300)])
            r.remember_detail(self.conn,'123',dict(body='old description',price_byn='35000'))
            r.remember_detail(self.conn,'123',dict(body='new description',price_byn='30000'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ad_revisions').fetchone()[0],4)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feed_inbox').fetchone()[0],1)

    def test_graveyard_malformed_200_does_not_mark_sold_or_gone(self):
        self.seed()
        settings = self.bot.DEFAULT_SETTINGS.copy()
        settings['last_digest_date'] = time.strftime('%Y-%m-%d',time.gmtime(time.time()+3*3600))
        with patch.object(self.bot.kufar_session,'get',return_value=response(data={'result':None})), \
             patch.object(self.bot,'handle_telegram_updates',return_value=0), patch.object(self.bot.time,'sleep'):
            self.bot.check_graveyard_and_digest(settings,0)
        self.assertEqual(self.conn.execute('SELECT status FROM lot_history').fetchone()[0],'active')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM availability_history').fetchone()[0],0)

    def test_graveyard_404_records_observation_without_transaction_price(self):
        self.seed()
        settings = self.bot.DEFAULT_SETTINGS.copy()
        settings['last_digest_date'] = time.strftime('%Y-%m-%d',time.gmtime(time.time()+3*3600))
        with patch.object(self.bot.kufar_session,'get',return_value=response(code=404)), \
             patch.object(self.bot,'handle_telegram_updates',return_value=0), patch.object(self.bot.time,'sleep'):
            self.bot.check_graveyard_and_digest(settings,0)
        row = self.conn.execute('SELECT status,estimated_sold_price,confirmed_sale_price FROM lot_history').fetchone()
        self.assertEqual(tuple(row),('unavailable',None,None))

    def test_restore_valid_snapshot_over_corrupt_working_file(self):
        self.seed()
        archive = self.snapshot()
        self.bot.close_db()
        self.db.write_bytes(b'corrupt working database')
        st.install_snapshot(archive,self.db,self.settings,overwrite=True)
        self.assertEqual(st.describe_db(self.db)['lot_history'],1)
        self.assertEqual(len(list((self.root/'backups').glob('unverified-before-restore-*.db'))),1)

    def test_retention_preserves_versions_and_manual_original(self):
        self.seed()
        template = self.snapshot().read_bytes()
        folder = self.root/'retention'
        folder.mkdir()
        for day in range(20,26):
            (folder/f'sniper-state-202611{day}T010000Z-aaaaaaaaaaaa.zip').write_bytes(template)
        original = folder/'pre-v40-original.zip'
        original.write_bytes(template)
        removed = st.prune_local(folder,keep_recent=3,keep_daily=2)
        self.assertEqual(removed,3)
        self.assertEqual(len(list(folder.glob('sniper-state-*.zip'))),3)
        self.assertTrue(original.exists())
        st.verify_snapshot(sorted(folder.glob('sniper-state-*.zip'))[-1],self.root/'verify')

    def test_retention_refuses_deletion_when_recent_copy_is_corrupt(self):
        self.seed()
        folder=self.root/'retention'
        folder.mkdir()
        template=self.snapshot().read_bytes()
        for day in range(20,26):
            (folder/f'sniper-state-202611{day}T010000Z-aaaaaaaaaaaa.zip').write_bytes(template)
        sorted(folder.iterdir())[-1].write_bytes(b'broken')
        with self.assertRaises(zipfile.BadZipFile):
            st.prune_local(folder,keep_recent=3,keep_daily=2)
        self.assertEqual(len(list(folder.iterdir())),6)

    def test_settings_change_cancels_stale_cards_but_allows_reevaluation(self):
        settings = self.bot.DEFAULT_SETTINGS.copy()
        r.settings_revision(self.conn,settings)
        with self.conn:
            r.remember_ads(self.conn,[self.ad()])
            r.queue_notification(self.conn,'123',300,'old card')
        r.finish_ad(self.conn,'123')
        r.settings_revision(self.conn,dict(settings,max_price=250))
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0],'cancelled')
        self.assertEqual(len(r.pending_ads(self.conn)),1)
        with self.conn:
            r.queue_notification(self.conn,'123',300,'recalculated card')
        self.assertEqual(self.conn.execute('SELECT state FROM notification_outbox').fetchone()[0],'pending')

    def test_data_audit_is_read_only(self):
        from data_audit import audit
        self.seed()
        before=st.sha256(self.db)
        result=audit(self.db)
        self.assertEqual(result['quality']['total'],1)
        self.assertEqual(result['quality']['confirmed_prices'],0)
        self.assertEqual(st.sha256(self.db),before)

    def test_existing_public_release_roundtrip_via_gh_adapter(self):
        self.seed()
        remote = FakeRelease()
        store = st.GhRelease('debon25/snipser')
        commands = []
        def gh(*args):
            commands.append(args)
            if args == ('api','repos/debon25/snipser'):
                return json.dumps({'private':False})
            if args == ('api','repos/debon25/snipser/releases/tags/db-state'):
                return json.dumps({'id':123})
            if args[:3] == ('api','--paginate','--slurp'):
                return json.dumps([remote.assets()])
            if args[:3] == ('release','upload','db-state'):
                remote.upload(args[3])
                return ''
            if args[:3] == ('release','download','db-state'):
                remote.download(args[args.index('-p')+1],args[args.index('-D')+1])
                return ''
            raise AssertionError('Unexpected CLI operation')
        with patch.object(store,'run',side_effect=gh):
            self.assertFalse(store.is_private())
            archive = self.snapshot()
            st.publish(store,archive)
            st.restore_release(store,self.root/'public-recovered.db',self.root/'public-settings.json',self.root/'baseline.db')
        st.compare_history(self.db,self.root/'public-recovered.db')
        self.assertTrue(any(name.endswith('.ready.json') for name in remote.files))
        self.assertFalse(any('--clobber' in args for args in commands))

    def test_unknown_state_visibility_is_not_assumed_public(self):
        store = st.GhRelease('owner/state')
        with patch.object(store,'run',return_value='{}'):
            with self.assertRaises(st.StateError):
                store.is_private()


if __name__ == '__main__':
    unittest.main(verbosity=2)
