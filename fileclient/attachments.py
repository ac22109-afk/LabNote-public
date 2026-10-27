"""Names remain in revision metadata; blob names are opaque storage identifiers."""
from pathlib import Path, PurePosixPath


def original_filename(attachment):
    return PurePosixPath(attachment['name'].replace('\\', '/')).name


def original_extension(attachment):
    name = original_filename(attachment)
    for extension in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst'):
        if name.lower().endswith(extension):
            return name[-len(extension):]
    return PurePosixPath(name).suffix


def export_destination(destination, attachment):
    """Supply a missing extension only. Never claim to convert file contents."""
    path = Path(destination)
    extension = original_extension(attachment)
    if extension and not path.suffix:
        path = path.with_name(path.name + extension)
    return path
