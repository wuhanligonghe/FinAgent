from hashlib import sha256
from importlib.resources import files


def package_digest():
    """Hash shipped implementation and resources; never read local .env/data."""
    digest = sha256()

    def visit(directory, prefix=""):
        for item in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = prefix + item.name
            if item.is_dir() and item.name != "__pycache__":
                visit(item, relative + "/")
            elif item.is_file() and item.name.endswith((".py", ".md", ".json", ".txt")):
                digest.update(relative.encode())
                digest.update(b"\0")
                digest.update(item.read_bytes())
    visit(files("finagent"))
    return digest.hexdigest()
