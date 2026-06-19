import os


def scan(root):
    return list(os.walk(root))
