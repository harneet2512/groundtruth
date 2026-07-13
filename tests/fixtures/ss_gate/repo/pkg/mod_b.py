"""Generic fixture module B for the SS gate (no benchmark repo, no task id)."""
from pkg.mod_a import alpha


def beta():
    return 2


def run():
    return "b"


def consumer():
    if alpha():
        return run()
    return None


def handler():
    from pkg.util import sig_target
    return sig_target(1)
