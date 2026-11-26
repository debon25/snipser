CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS lot_history (
            ad_id TEXT PRIMARY KEY,
            account_id TEXT,
            model TEXT,
            memory INTEGER,
            battery_health INTEGER,
            color TEXT,
            district TEXT,
            has_box INTEGER,
            has_defects INTEGER DEFAULT 0,
            first_seen_price REAL,
            last_seen_price REAL,
            last_drop_ref_price REAL,
            price_drops_count INTEGER DEFAULT 0,
            first_seen_ts INTEGER,
            last_seen_ts INTEGER,
            list_time_ts INTEGER,
            status TEXT DEFAULT 'active',
            days_on_market REAL DEFAULT 0,
            estimated_sold_price REAL,
            notified INTEGER DEFAULT 0,
            title TEXT,
            description TEXT
        );

CREATE TABLE IF NOT EXISTS market_cache (
            cache_key TEXT PRIMARY KEY,
            market_price REAL,
            min_price REAL,
            min_link TEXT,
            sample_size INTEGER,
            median_days REAL,
            resellers_count INTEGER,
            is_correlated INTEGER DEFAULT 0,
            updated_ts INTEGER
        );

CREATE TABLE IF NOT EXISTS ml_ads (
            ad_id TEXT PRIMARY KEY,
            account_id TEXT,
            subject TEXT,
            body TEXT,
            price_byn REAL,
            first_price_byn REAL,
            price_usd REAL,
            currency TEXT,
            company_ad INTEGER,
            images_count INTEGER,
            region TEXT,
            district TEXT,
            ad_parameters_json TEXT,
            account_parameters_json TEXT,
            paid_services_json TEXT,
            model TEXT,
            memory INTEGER,
            is_assumed_mem INTEGER,
            battery_health INTEGER,
            color TEXT,
            has_box INTEGER,
            has_defects INTEGER,
            status TEXT,
            ban_reason TEXT,
            list_time_ts INTEGER,
            first_seen_ts INTEGER,
            last_seen_ts INTEGER,
            sold_ts INTEGER,
            days_on_market REAL,
            estimated_sold_price REAL,
            price_drops_count INTEGER DEFAULT 0,
            ad_link TEXT
        );

CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            price_byn REAL NOT NULL,
            ts INTEGER NOT NULL,
            source TEXT DEFAULT 'feed'
        );