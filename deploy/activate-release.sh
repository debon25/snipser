#!/usr/bin/env bash
# Same script activates a previously prepared v4 release for code rollback.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }
release=$(realpath -- "${1:?Pass the prepared release directory}")
[[ $release == /opt/kufar-sniper/releases/* && -f $release/runner.py && -x $release/.venv/bin/python ]] || { echo 'Invalid release path'; exit 1; }
[[ -s /etc/kufar-sniper.env ]] || { echo 'Configure /etc/kufar-sniper.env first'; exit 1; }
[[ -s /var/lib/kufar-sniper/sniper.db ]] || { echo 'Restore the database first; automatic empty initialization is refused'; exit 1; }
systemctl stop kufar-sniper-backup.timer kufar-sniper-health.timer kufar-sniper-backup.service kufar-sniper-health.service kufar-sniper.service || true
if systemctl is-active --quiet kufar-sniper.service; then
  echo 'The previous bot is still active; activation stopped'
  exit 1
fi
"$release/.venv/bin/python" "$release/state_store.py" snapshot --db /var/lib/kufar-sniper/sniper.db --settings /var/lib/kufar-sniper/settings.json --output /var/lib/kufar-sniper/backups
chown -R kufar:kufar /var/lib/kufar-sniper
if [[ -L /opt/kufar-sniper/current ]]; then
  old_release=$(readlink -f /opt/kufar-sniper/current)
  ln -sfn -- "$old_release" /opt/kufar-sniper/previous
fi
ln -sfn -- "$release" /opt/kufar-sniper/current.next
mv -Tf -- /opt/kufar-sniper/current.next /opt/kufar-sniper/current
systemctl daemon-reload
systemctl enable --now kufar-sniper.service kufar-sniper-backup.timer kufar-sniper-health.timer
systemctl --no-pager status kufar-sniper.service
