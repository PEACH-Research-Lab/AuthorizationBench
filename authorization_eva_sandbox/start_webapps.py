"""
Reads task_config.json and starts the required web app templates.
Each web app runs as a subprocess on its configured port.
"""
import json
import os
import subprocess
import sys
import time

TASK_CONFIG_PATH = os.environ.get("TASK_CONFIG_PATH", "/config/task_config.json")

WEBAPP_MODULES = {
    "email_client": "webapps.email_client.app",
    "shopping_app_instacart": "webapps.shopping_app_instacart.app",
    "amazon_shop": "webapps.amazon_shop.app",
    "calendar": "webapps.calendar.app",
    "docs_portal": "webapps.docs_portal.app",
    "zoom_desktop": "webapps.zoom_desktop.app",
    "zoom_settings": "webapps.zoom_settings.app",
    "dev_forum": "webapps.dev_forum.app",
    "github_repo": "webapps.github_repo.app",
    "tenant_portal": "webapps.tenant_portal.app",
    "etrade_broker": "webapps.etrade_broker.app",
    "slack_workspace": "webapps.slack_workspace.app",
    "customer_support_console": "webapps.customer_support_console.app",
    "school_directory": "webapps.school_directory.app",
    "wechat_app": "webapps.wechat_app.app",
    "thomasnet_marketplace": "webapps.thomasnet_marketplace.app",
    "repo_aggregator": "webapps.repo_aggregator.app",
    "quickgist_ai": "webapps.quickgist_ai.app",
    "sales_analysis_site": "webapps.sales_analysis_site.app",
    "discord_app": "webapps.discord_app.app",
    "crypto_wallet": "webapps.crypto_wallet.app",
    "identity_survey": "webapps.identity_survey.app",
    "class_trip_form": "webapps.class_trip_form.app",
    "saved_info": "webapps.saved_info.app",
    "teacher_connect": "webapps.teacher_connect.app",
}


def start_webapps():
    if not os.path.exists(TASK_CONFIG_PATH):
        print(f"No task_config.json found at {TASK_CONFIG_PATH}, no web apps to start.")
        return []

    with open(TASK_CONFIG_PATH) as f:
        config = json.load(f)

    processes = []
    for platform in config.get("platforms", []):
        template = platform["template"]
        port = platform["port"]
        data_path = platform.get("data_path", "")

        module = WEBAPP_MODULES.get(template)
        if module is None:
            print(f"Warning: No module registered for template '{template}', skipping.")
            continue

        env = os.environ.copy()
        env["WEBAPP_PORT"] = str(port)
        env["WEBAPP_DATA_PATH"] = data_path

        # Webapp-private config (NOT agent-visible): test-mechanism data such as
        # scripted colleague replies must live outside /data so a bash-capable
        # agent cannot read the scripted trap. Convention: run_eval copies
        # tasks/<T>/webapp_private/ -> /config/webapp_private/.
        private_path = f"/config/webapp_private/{template}_private.json"
        if os.path.exists(private_path):
            env["WEBAPP_PRIVATE_DATA_PATH"] = private_path

        # Zoom desktop & settings share a small JSON store for live toggles.
        if template in ("zoom_desktop", "zoom_settings"):
            env.setdefault("ZOOM_SETTINGS_PATH", "/data/zoom_settings_state.json")

        python_bin = "/usr/local/bin/python_default"
        if not os.path.exists(python_bin):
            python_bin = sys.executable

        print(f"Starting {template} on port {port} (data: {data_path})")
        proc = subprocess.Popen(
            [python_bin, "-m", module],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append((template, proc))

    return processes


if __name__ == "__main__":
    procs = start_webapps()
    if not procs:
        print("No web apps started.")
        sys.exit(0)

    print(f"Started {len(procs)} web app(s). Waiting for them...")
    try:
        while True:
            for name, proc in procs:
                ret = proc.poll()
                if ret is not None:
                    print(f"Web app '{name}' exited with code {ret}")
                    stdout, stderr = proc.communicate()
                    if stderr:
                        print(f"  stderr: {stderr.decode()[:500]}")
            time.sleep(5)
    except KeyboardInterrupt:
        for name, proc in procs:
            proc.terminate()
