#!/usr/bin/env bash
# Run on the selected Ubuntu/Debian server only after hosting is approved.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }
version=${1:-v4.0.1}
[[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Expected a version tag such as v4.0.1'; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
commit=$(git -C "$source_dir" rev-parse --verify "${version}^{commit}")
release="/opt/kufar-sniper/releases/${version}-${commit:0:12}"
[[ ! -e $release ]] || { echo "Release already prepared: $release"; exit 1; }
apt-get update
apt-get install -y python3 python3-venv git
id kufar >/dev/null 2>&1 || useradd --system --home /var/lib/kufar-sniper --shell /usr/sbin/nologin kufar
install -d -m 755 /opt/kufar-sniper/releases "$release"
install -d -o kufar -g kufar -m 700 /var/lib/kufar-sniper
git -C "$source_dir" archive "$version" | tar -x -C "$release"
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install -r "$release/requirements-dev.txt"
(cd "$release" && "$release/.venv/bin/python" -m unittest discover -s tests -v)
install -m 644 "$release"/deploy/*.service "$release"/deploy/*.timer /etc/systemd/system/
if [[ ! -e /etc/kufar-sniper.env ]]; then
  install -m 600 "$release/.env.example" /etc/kufar-sniper.env
fi
echo "Prepared: $release"
echo 'No bot was started. Restore the production database and configure /etc/kufar-sniper.env before activation.'
