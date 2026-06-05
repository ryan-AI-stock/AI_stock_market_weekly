"""Small logging facade for weekly report jobs.

Keep output text stable while centralizing where console messages are emitted.
"""


def log(message: str = "", *, end: str = "\n") -> None:
    print(message, end=end)


def warn(message: str) -> None:
    log(f"⚠️  {message}")


def error(message: str) -> None:
    log(f"❌ {message}")


def success(message: str) -> None:
    log(f"✅ {message}")
