"""Python fixture: bare callable alias + parameter callback + FastAPI route."""
from fastapi import Depends, FastAPI

app = FastAPI()


def get_db():
    yield None


def helper() -> str:
    return "ok"


def save_record(record: str) -> bool:
    return bool(record)


# Bare alias: alias_helper() must resolve to helper via callable_value.
alias_helper = helper


def run_alias() -> str:
    return alias_helper()


# Parameter callback: wrap(helper) then cb() must flow arg->formal.
def wrap(cb):
    return cb()


def entry() -> str:
    return wrap(helper)


@app.get("/api/items")
def list_items(db=Depends(get_db)) -> dict:
    return {"items": run_alias()}


# Field-access shapes: READS/WRITES edges must carry statement-level
# access_sites (per-site line + read/write kind).
class Counter:
    def __init__(self) -> None:
        self.count = 0
        self.label = "counter"

    def bump(self) -> int:
        self.count += 1
        return self.count

    def describe(self) -> str:
        return f"{self.label}:{self.count}"
