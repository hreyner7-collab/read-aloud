"""Short, shared wording for the floating button's right-click menus."""
from . import settings as _settings


def voice_title(settings):
    current = settings.get("voice")
    return "Voice · " + next((label for vid, label in _settings.VOICES if vid == current),
                             "Sonia")


def speed_title(settings):
    current = settings.get("rate")
    return "Speed · " + next((label for rate, label in _settings.SPEEDS if rate == current),
                             "Normal")


def status_line(text, limit=74):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def update_action(updater):
    if updater.state == "ready":
        return "Install update"
    if getattr(updater, "staged", None):
        return "Try installing again"
    return "Check now"


def update_status(updater):
    text = updater.status_text()
    if (getattr(updater, "auto", False) and updater.supported
            and "daily" not in text.lower()):
        text += " · daily check"
    return status_line(text, 84)


def offline_status(controller):
    text = controller.offline_status()
    return text[0].upper() + text[1:] if text else "Offline voice status unknown"


CACHE_NOTE = "Audio only — never your text (64 MB)"
DISK_LABEL = "Keep recent audio on disk"
OFFLINE_LABEL = "Offline voice only"
DOWNLOAD_LABEL = "Download offline voice"
HOW_TO_LABEL = "How to use"
