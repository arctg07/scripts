"""Настройки: корень проектов, рабочие каталоги, лимиты.

Порядок (каждый следующий перекрывает предыдущий):
  значения по умолчанию → config.json → config.local.json → переменные окружения.

config.local.json и рабочие каталоги (inbox/, export/, backups/) у каждой машины свои
и в git не попадают.
"""

import json
import os

# realpath: при запуске через симлинк корнем остаётся реальный родитель папки scripts.
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

DEFAULTS = {
    # None — родительский каталог папки scripts (она лежит в корне рядом с проектами).
    "projects_root": None,
    # Максимум строк в одном файле-части, включая строку заголовка.
    "max_lines": 7000,
    "port": 8765,
    # Каталоги в корне, которые не показываются в списке проектов.
    "hidden_projects": [],
    # Имя проекта в выгрузке -> имя локального проекта, если на машинах они различаются.
    "aliases": {},
    # Сколько последних бэкапов хранить на проект.
    "keep_backups": 30,
}


class Config(object):
    def __init__(self, data, scripts_dir, data_dir):
        self.scripts_dir = scripts_dir
        root = data.get("projects_root") or os.path.dirname(scripts_dir)
        self.projects_root = os.path.abspath(os.path.expanduser(root))
        self.data_dir = data_dir
        self.inbox_dir = os.path.join(data_dir, "inbox")
        self.export_dir = os.path.join(data_dir, "export")
        self.backups_dir = os.path.join(data_dir, "backups")
        self.max_lines = max(10, int(data.get("max_lines") or DEFAULTS["max_lines"]))
        self.port = int(data.get("port") or DEFAULTS["port"])
        self.hidden_projects = set(data.get("hidden_projects") or [])
        self.aliases = dict(data.get("aliases") or {})
        self.keep_backups = max(1, int(data.get("keep_backups") or DEFAULTS["keep_backups"]))

    def local_name(self, project):
        """Имя локального проекта для имени из выгрузки (с учётом aliases)."""
        return self.aliases.get(project, project)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("%s: ожидается JSON-объект" % path)
    return data


def load_config(scripts_dir=None):
    scripts_dir = scripts_dir or SCRIPTS_DIR
    data = dict(DEFAULTS)
    for name in ("config.json", "config.local.json"):
        path = os.path.join(scripts_dir, name)
        if os.path.isfile(path):
            data.update(_read_json(path))

    env = os.environ
    if env.get("TRANSFER_PROJECTS_ROOT"):
        data["projects_root"] = env["TRANSFER_PROJECTS_ROOT"]
    if env.get("TRANSFER_MAX_LINES"):
        data["max_lines"] = int(env["TRANSFER_MAX_LINES"])
    data_dir = os.path.abspath(os.path.expanduser(env.get("TRANSFER_DATA_DIR") or scripts_dir))
    return Config(data, scripts_dir, data_dir)
