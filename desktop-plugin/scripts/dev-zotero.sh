#!/usr/bin/env bash
# Runs a throwaway, headless Zotero 10 with this plugin loaded (unpacked from build/), on its own
# profile and data directory, so it never touches a real Zotero setup or the screen.
#   scripts/dev-zotero.sh start | stop | restart | status
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZOTERO_BIN="${ZOTERO_BIN:-$HOME/Applications/zotero-10/Zotero_linux-x86_64/zotero}"
BASE="${ZOTERO_AI_TEST_HOME:-$HOME/.zotero-ai-test}"
PORT="${ZOTERO_AI_PORT:-23119}"
PROFILE="$BASE/profile"
DATA="$BASE/data"
PIDFILE="$BASE/zotero.pid"
LOG="$BASE/zotero.log"
TOKENFILE="$BASE/test-token"
ADDON_ID="zotero-ai@zotero-android"

case "$(realpath -m "$DATA")" in "$(realpath -m "$HOME/Zotero")"*) echo "refusing: data dir is the real Zotero data dir" >&2; exit 2;; esac

# The launcher script starts zotero-bin as a child, so find the real process by its profile.
running() { pgrep -f -- "zotero-bin .*-profile $PROFILE( |$)" >/dev/null; }

write_profile() {
	mkdir -p "$PROFILE/extensions" "$DATA"
	printf '%s' "$ROOT/build" > "$PROFILE/extensions/$ADDON_ID"
	cat > "$PROFILE/user.js" <<PREFS
user_pref("extensions.zotero.dataDir", "$DATA");
user_pref("extensions.zotero.useDataDir", true);
user_pref("extensions.zotero.firstRun2", false);
user_pref("extensions.zotero.firstRunGuidance", false);
user_pref("extensions.zotero.httpServer.enabled", true);
user_pref("extensions.zotero.httpServer.port", $PORT);
user_pref("extensions.zotero.sync.storage.enabled", false);
user_pref("extensions.zotero.sync.storage.groups.enabled", false);
user_pref("extensions.zotero.sync.fulltext.enabled", true);
user_pref("extensions.zotero.sync.autoSync", false);
user_pref("extensions.zotero.automaticScraperUpdates", false);
user_pref("extensions.zoteroOpenOfficeIntegration.skipInstallation", true);
user_pref("extensions.zoteroOpenOfficeIntegration.installed", true);
user_pref("extensions.zotero-ai.testEndpoints", true);
user_pref("extensions.startupScanScopes", 1);
user_pref("extensions.autoDisableScopes", 0);
user_pref("extensions.enabledScopes", 15);
user_pref("extensions.update.enabled", false);
user_pref("app.update.enabled", false);
user_pref("app.update.disabledForTesting", true);
user_pref("browser.dom.window.dump.enabled", true);
user_pref("devtools.console.stdout.chrome", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
PREFS
	# Re-register the unpacked add-on on every start.
	rm -f "$PROFILE/extensions.json" "$PROFILE/addonStartup.json.lz4"
	rm -rf "$PROFILE/startupCache"
}

start() {
	running && { echo "already running (pid $(cat "$PIDFILE"))"; return; }
	node "$ROOT/scripts/build.mjs" >/dev/null
	write_profile
	# A fresh secret per launch for the dev endpoints; tests read it from $TOKENFILE.
	(umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKENFILE")
	ZOTERO_AI_TEST_TOKEN="$(cat "$TOKENFILE")" MOZ_HEADLESS=1 MOZ_HEADLESS_WIDTH=1600 MOZ_HEADLESS_HEIGHT=1000 \
		nohup "$ZOTERO_BIN" -profile "$PROFILE" -no-remote -purgecaches >"$LOG" 2>&1 &
	echo $! > "$PIDFILE"
	for _ in $(seq 1 90); do
		if curl -sf -o /dev/null -H "X-Zotero-AI-Test-Token: $(cat "$TOKENFILE")" "http://127.0.0.1:$PORT/zotero-ai/test/status"; then echo "started (pid $(cat "$PIDFILE"), port $PORT)"; return; fi
		running || { echo "zotero exited; see $LOG" >&2; exit 1; }
		sleep 1
	done
	echo "timed out waiting for the test endpoint; see $LOG" >&2; exit 1
}

stop() {
	if running; then
		pkill -f -- "zotero-bin .*-profile $PROFILE( |$)" || true
		for _ in $(seq 1 20); do running || break; sleep 0.5; done
		running && pkill -9 -f -- "zotero-bin .*-profile $PROFILE( |$)" || true
	fi
	rm -f "$PIDFILE"; echo stopped
}

case "${1:-status}" in
	start) start ;;
	stop) stop ;;
	restart) stop; start ;;
	status) running && echo "running (pid $(cat "$PIDFILE"))" || echo "not running" ;;
	*) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
