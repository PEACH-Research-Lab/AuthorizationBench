import os

SERVER_HOSTNAME = os.getenv("SERVER_HOSTNAME", "the-agent-company.com")

# LLM Config (for Oracle User, Interactive NPCs, and any LLM-based evaluation)
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "https://api.openai.com/v1")
LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "gpt-4o")

# GitLab Config (shared service, reused from OAS)
GITLAB_PORT = os.getenv("GITLAB_PORT", "8929")
GITLAB_BASEURL = f"http://{SERVER_HOSTNAME}:{GITLAB_PORT}"
GITLAB_ACCESS_TOKEN = "root-token"
GITLAB_HEADERS = {"PRIVATE-TOKEN": GITLAB_ACCESS_TOKEN}
GITLAB_USER = "root"

# API Server Config (shared service reset endpoint)
API_SERVER_PORT = os.getenv("API_SERVER_PORT", "2999")
API_SERVER_URL = f"http://{SERVER_HOSTNAME}:{API_SERVER_PORT}"

# Web App Template Ports (inside per-task Docker container)
WEBAPP_PORTS = {
    "email_client": 5001,
    "qa_forum": 5002,
    "chat_messaging": 5003,
    "file_manager": 5004,
    "shopping_site": 5005,
    "medical_portal": 5006,
    "slack_workspace": 5009,
    "school_directory": 5010,
    "wechat_app": 5012,
    "discord_app": 5014,
    "crypto_wallet": 5015,
}

# Paths inside the Docker container
INSTRUCTION_DIR = "/instruction"
WORKSPACE_DIR = "/workspace"
UTILS_DIR = "/utils"
NPC_DIR = "/npc"
CONFIG_DIR = "/config"
DATA_DIR = "/data"
OUTPUTS_DIR = "/outputs"

TASK_CONFIG_PATH = os.path.join(CONFIG_DIR, "task_config.json")
