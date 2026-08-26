"""Carbon downloadable-content entitlement catalog and account assignments.

Retail Carbon receives internal unlock names through the DOBJ
``GetObjectInventory`` response.  The client validates ``dateEntitled`` and
hashes ``objectId`` directly before inserting it into the online DLC inventory.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import blake2b
import json
import os
from pathlib import Path
from threading import RLock
import time
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from carbon.accounts.identity import Identity


DEFAULT_DATE_ENTITLED = "Jan-1-2007 0:00:00 UTC"
_MAX_GROUPS = 1_000
_MAX_PRESETS = 1_000
_MAX_TOKENS = 20_000
_MAX_SELECTORS = 2_000
_MAX_TEXT = 256


class CarbonDLCConfigError(ValueError):
    """Raised when a DLC catalog or assignment document is malformed."""


@dataclass(frozen=True)
class CarbonDLCGroup:
    key: str
    label: str
    category: str
    tokens: tuple[str, ...]
    source_file: str = ""


@dataclass(frozen=True)
class CarbonDLCCatalog:
    groups: Mapping[str, CarbonDLCGroup]
    presets: Mapping[str, tuple[str, ...]]

    @classmethod
    def compatibility_default(cls) -> "CarbonDLCCatalog":
        group = CarbonDLCGroup(
            key="virus_pursuit_pandemic",
            label="Virus Pursuit Pandemic",
            category="vinyls",
            tokens=("VIRUS_PURSUIT_PANDEMIC",),
        )
        return cls(
            groups={group.key: group},
            presets={"default_dlc": (group.key,)},
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "CarbonDLCCatalog":
        config_path = Path(path)
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CarbonDLCConfigError(
                f"cannot read Carbon DLC catalog {config_path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise CarbonDLCConfigError(
                f"invalid JSON in Carbon DLC catalog {config_path}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise CarbonDLCConfigError("Carbon DLC catalog root must be an object")
        if document.get("version") != 1:
            raise CarbonDLCConfigError("Carbon DLC catalog version must be 1")

        raw_groups = document.get("groups")
        if not isinstance(raw_groups, dict) or not raw_groups:
            raise CarbonDLCConfigError("Carbon DLC catalog groups must be a non-empty object")
        if len(raw_groups) > _MAX_GROUPS:
            raise CarbonDLCConfigError(f"Carbon DLC catalog exceeds {_MAX_GROUPS} groups")

        groups: dict[str, CarbonDLCGroup] = {}
        raw_token_count = 0
        for raw_key, raw_group in raw_groups.items():
            key = _clean_identifier(raw_key, field="group key")
            _reject_reserved_selector_name(key, field="group key")
            if key in groups:
                raise CarbonDLCConfigError(f"duplicate Carbon DLC group {key!r}")
            if not isinstance(raw_group, dict):
                raise CarbonDLCConfigError(f"Carbon DLC group {key!r} must be an object")
            label = _clean_text(raw_group.get("label", key), field=f"group {key} label")
            category = _clean_identifier(
                raw_group.get("category", "other"),
                field=f"group {key} category",
            )
            source_file = _clean_optional_text(
                raw_group.get("source_file", ""),
                field=f"group {key} source_file",
            )
            raw_tokens = raw_group.get("tokens")
            if not isinstance(raw_tokens, list) or not raw_tokens:
                raise CarbonDLCConfigError(
                    f"Carbon DLC group {key!r} tokens must be a non-empty array"
                )
            tokens = tuple(
                _clean_token(value, field=f"group {key} token")
                for value in raw_tokens
            )
            raw_token_count += len(tokens)
            if raw_token_count > _MAX_TOKENS:
                raise CarbonDLCConfigError(
                    f"Carbon DLC catalog exceeds {_MAX_TOKENS} raw tokens"
                )
            groups[key] = CarbonDLCGroup(
                key=key,
                label=label,
                category=category,
                tokens=tokens,
                source_file=source_file,
            )

        raw_presets = document.get("presets", {})
        if not isinstance(raw_presets, dict):
            raise CarbonDLCConfigError("Carbon DLC catalog presets must be an object")
        if len(raw_presets) > _MAX_PRESETS:
            raise CarbonDLCConfigError(f"Carbon DLC catalog exceeds {_MAX_PRESETS} presets")
        presets: dict[str, tuple[str, ...]] = {}
        for raw_key, raw_selectors in raw_presets.items():
            key = _clean_identifier(raw_key, field="preset key")
            _reject_reserved_selector_name(key, field="preset key")
            if key in groups:
                raise CarbonDLCConfigError(
                    f"Carbon DLC preset {key!r} conflicts with a group key"
                )
            if not isinstance(raw_selectors, list):
                raise CarbonDLCConfigError(
                    f"Carbon DLC preset {key!r} must be an array"
                )
            presets[key] = _selector_tuple(raw_selectors, field=f"preset {key}")

        catalog = cls(groups=groups, presets=presets)
        # Validate references and recursive cycles during startup rather than
        # failing only when one specific account requests its inventory.
        for preset in presets:
            catalog.expand((preset,))
        return catalog

    def all_tokens(self) -> tuple[str, ...]:
        return _deduplicate(
            token
            for group in self.groups.values()
            for token in group.tokens
        )

    def expand(self, selectors: Sequence[str]) -> tuple[str, ...]:
        if len(selectors) > _MAX_SELECTORS:
            raise CarbonDLCConfigError(
                f"Carbon DLC selection exceeds {_MAX_SELECTORS} selectors"
            )
        included: list[str] = []
        excluded: list[str] = []
        for raw_selector in selectors:
            selector = _clean_selector(raw_selector)
            negative = selector.startswith("-")
            target = selector[1:] if negative else selector
            tokens = self._expand_one(target, stack=())
            (excluded if negative else included).extend(tokens)
        blocked = set(excluded)
        return tuple(token for token in _deduplicate(included) if token not in blocked)

    def _expand_one(self, selector: str, *, stack: tuple[str, ...]) -> tuple[str, ...]:
        if selector == "none":
            return ()
        if selector == "all":
            return self.all_tokens()
        if selector.startswith("token:"):
            return (_clean_token(selector[6:], field="raw token selector"),)
        group = self.groups.get(selector)
        if group is not None:
            return group.tokens
        preset = self.presets.get(selector)
        if preset is not None:
            if selector in stack:
                cycle = " -> ".join((*stack, selector))
                raise CarbonDLCConfigError(f"Carbon DLC preset cycle: {cycle}")
            expanded: list[str] = []
            for child in preset:
                negative = child.startswith("-")
                target = child[1:] if negative else child
                child_tokens = self._expand_one(target, stack=(*stack, selector))
                if negative:
                    blocked = set(child_tokens)
                    expanded = [token for token in expanded if token not in blocked]
                else:
                    expanded.extend(child_tokens)
            return _deduplicate(expanded)
        raise CarbonDLCConfigError(f"unknown Carbon DLC selector {selector!r}")


@dataclass(frozen=True)
class CarbonDLCAssignments:
    default: tuple[str, ...]
    accounts: Mapping[str, tuple[str, ...]]
    personas: Mapping[str, tuple[str, ...]]

    @classmethod
    def compatibility_default(cls) -> "CarbonDLCAssignments":
        return cls(default=("default_dlc",), accounts={}, personas={})

    @classmethod
    def from_path(cls, path: str | Path) -> "CarbonDLCAssignments":
        config_path = Path(path)
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CarbonDLCConfigError(
                f"cannot read Carbon DLC assignments {config_path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise CarbonDLCConfigError(
                f"invalid JSON in Carbon DLC assignments {config_path}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise CarbonDLCConfigError("Carbon DLC assignments root must be an object")
        if document.get("version") != 1:
            raise CarbonDLCConfigError("Carbon DLC assignments version must be 1")
        default = _selector_tuple(document.get("default", []), field="default assignment")
        accounts = _assignment_map(document.get("accounts", {}), field="accounts")
        personas = _assignment_map(document.get("personas", {}), field="personas")
        return cls(default=default, accounts=accounts, personas=personas)

    def selectors_for(self, identity: Identity | None) -> tuple[str, ...]:
        if identity is None:
            return self.default
        account = self.accounts.get(identity.account_name.casefold())
        if account is not None:
            return account
        persona = self.personas.get(identity.persona.casefold())
        if persona is not None:
            return persona
        return self.default


class CarbonDLCAssignmentStore:
    """Process-safe assignment file with atomic writes and hot reloads."""

    def __init__(
        self,
        path: str | Path,
        catalog: CarbonDLCCatalog,
        *,
        lock_timeout: float = 5.0,
        stale_lock_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self.catalog = catalog
        self.lock_timeout = max(0.1, float(lock_timeout))
        self.stale_lock_seconds = max(self.lock_timeout, float(stale_lock_seconds))
        self._lock = RLock()
        self._signature: tuple[int, int] | None = None
        self._initialize_missing_file()
        self._assignments = CarbonDLCAssignments.from_path(self.path)
        self._validate(self._assignments)
        self._signature = self._file_signature()

    def _initial_assignments(self) -> CarbonDLCAssignments:
        selectors = (
            ("default_dlc",)
            if "default_dlc" in self.catalog.presets
            else ("none",)
        )
        return CarbonDLCAssignments(default=selectors, accounts={}, personas={})

    def _initialize_missing_file(self) -> None:
        """Create sanitized first-run state without clobbering another process."""

        if self.path.exists():
            return
        assignments = self._initial_assignments()
        document = {
            "version": 1,
            "default": list(assignments.default),
            "accounts": {},
            "personas": {},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return
        except OSError as exc:
            raise CarbonDLCConfigError(
                f"cannot initialize Carbon DLC assignments {self.path}: {exc}"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            self.path.unlink(missing_ok=True)
            raise CarbonDLCConfigError(
                f"cannot initialize Carbon DLC assignments {self.path}: {exc}"
            ) from exc

    def _file_signature(self) -> tuple[int, int]:
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise CarbonDLCConfigError(
                f"cannot stat Carbon DLC assignments {self.path}: {exc}"
            ) from exc
        return int(stat.st_mtime_ns), int(stat.st_size)

    def _validate(self, assignments: CarbonDLCAssignments) -> None:
        self.catalog.expand(assignments.default)
        for selectors in assignments.accounts.values():
            self.catalog.expand(selectors)
        for selectors in assignments.personas.values():
            self.catalog.expand(selectors)

    def current(self) -> CarbonDLCAssignments:
        with self._lock:
            signature = self._file_signature()
            if signature != self._signature:
                assignments = CarbonDLCAssignments.from_path(self.path)
                self._validate(assignments)
                self._assignments = assignments
                self._signature = signature
            return self._assignments

    @staticmethod
    def _account_key(account_name: object) -> str:
        return _clean_text(account_name, field="account assignment").casefold()

    def selectors_for_account(self, account_name: object) -> tuple[str, ...]:
        assignments = self.current()
        return assignments.accounts.get(
            self._account_key(account_name),
            assignments.default,
        )

    def set_account(
        self,
        account_name: object,
        selectors: Sequence[str],
    ) -> CarbonDLCAssignments:
        key = self._account_key(account_name)
        normalized = tuple(_clean_selector(value) for value in selectors)
        if not normalized:
            normalized = ("none",)
        self.catalog.expand(normalized)

        def mutate(current: CarbonDLCAssignments) -> CarbonDLCAssignments:
            accounts = dict(current.accounts)
            accounts[key] = normalized
            return CarbonDLCAssignments(
                default=current.default,
                accounts=accounts,
                personas=dict(current.personas),
            )

        return self._mutate(mutate)

    def reset_account(self, account_name: object) -> CarbonDLCAssignments:
        key = self._account_key(account_name)

        def mutate(current: CarbonDLCAssignments) -> CarbonDLCAssignments:
            accounts = dict(current.accounts)
            accounts.pop(key, None)
            return CarbonDLCAssignments(
                default=current.default,
                accounts=accounts,
                personas=dict(current.personas),
            )

        return self._mutate(mutate)

    def effective_group_keys(self, account_name: object) -> tuple[str, ...]:
        tokens = set(self.catalog.expand(self.selectors_for_account(account_name)))
        return tuple(
            key
            for key, group in self.catalog.groups.items()
            if set(group.tokens).issubset(tokens)
        )

    def _mutate(
        self,
        callback: Callable[[CarbonDLCAssignments], CarbonDLCAssignments],
    ) -> CarbonDLCAssignments:
        with self._lock:
            with self._file_lock():
                current = CarbonDLCAssignments.from_path(self.path)
                updated = callback(current)
                self._validate(updated)
                self._atomic_write(updated)
                self._assignments = updated
                self._signature = self._file_signature()
                return updated

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        deadline = time.monotonic() + self.lock_timeout
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age >= self.stale_lock_seconds:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise CarbonDLCConfigError(
                        f"timed out waiting for Carbon DLC assignment lock {lock_path}"
                    )
                time.sleep(0.05)
            except OSError as exc:
                raise CarbonDLCConfigError(
                    f"cannot lock Carbon DLC assignments {self.path}: {exc}"
                ) from exc
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            yield
        finally:
            try:
                os.close(descriptor)
            finally:
                lock_path.unlink(missing_ok=True)

    def _atomic_write(self, assignments: CarbonDLCAssignments) -> None:
        document = {
            "version": 1,
            "default": list(assignments.default),
            "accounts": {
                key: list(value)
                for key, value in sorted(assignments.accounts.items())
            },
            "personas": {
                key: list(value)
                for key, value in sorted(assignments.personas.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CarbonDLCConfigError(
                f"cannot write Carbon DLC assignments {self.path}: {exc}"
            ) from exc


@dataclass(frozen=True)
class CarbonDLCInventory:
    catalog: CarbonDLCCatalog
    assignments: CarbonDLCAssignments
    date_entitled: str = DEFAULT_DATE_ENTITLED
    seed_viral_carriers: bool = True
    assignment_store: CarbonDLCAssignmentStore | None = None

    @classmethod
    def compatibility_default(cls) -> "CarbonDLCInventory":
        return cls(
            catalog=CarbonDLCCatalog.compatibility_default(),
            assignments=CarbonDLCAssignments.compatibility_default(),
            seed_viral_carriers=False,
        )

    @classmethod
    def from_paths(
        cls,
        catalog_path: str | Path,
        assignments_path: str | Path,
    ) -> "CarbonDLCInventory":
        catalog = CarbonDLCCatalog.from_path(catalog_path)
        assignment_store = CarbonDLCAssignmentStore(assignments_path, catalog)
        assignments = assignment_store.current()
        inventory = cls(
            catalog=catalog,
            assignments=assignments,
            assignment_store=assignment_store,
        )
        inventory.validate_assignments()
        return inventory

    def current_assignments(self) -> CarbonDLCAssignments:
        if self.assignment_store is not None:
            return self.assignment_store.current()
        return self.assignments

    def validate_assignments(self) -> None:
        assignments = self.current_assignments()
        self.catalog.expand(assignments.default)
        for selectors in assignments.accounts.values():
            self.catalog.expand(selectors)
        for selectors in assignments.personas.values():
            self.catalog.expand(selectors)

    def tokens_for(
        self,
        identity: Identity | None,
        extra_tokens: Iterable[str] = (),
    ) -> tuple[str, ...]:
        assignments = self.current_assignments()
        static_tokens = self.catalog.expand(assignments.selectors_for(identity))
        return _deduplicate((*static_tokens, *extra_tokens))

    def fields_for(
        self,
        identity: Identity | None,
        extra_tokens: Iterable[str] = (),
    ) -> dict[str, str]:
        tokens = self.tokens_for(identity, extra_tokens)
        fields = {
            "TXN": "GetObjectInventory",
            "entitlements.[]": str(len(tokens)),
        }
        for index, token in enumerate(tokens):
            prefix = f"entitlements.{index}"
            fields[f"{prefix}.objectId"] = token
            fields[f"{prefix}.entitleId"] = str(_stable_entitlement_id(token))
            fields[f"{prefix}.dateEntitled"] = self.date_entitled
        return fields


def _stable_entitlement_id(token: str) -> int:
    value = int.from_bytes(
        blake2b(token.encode("utf-8"), digest_size=8, person=b"NFSC-DLC").digest(),
        "big",
    ) & 0x7FFF_FFFF_FFFF_FFFF
    return value or 1


def _assignment_map(value: object, *, field: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise CarbonDLCConfigError(f"Carbon DLC assignment {field} must be an object")
    result: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_selectors in value.items():
        name = _clean_text(raw_name, field=f"{field} key").casefold()
        if name in result:
            raise CarbonDLCConfigError(f"duplicate Carbon DLC {field} key {raw_name!r}")
        result[name] = _selector_tuple(raw_selectors, field=f"{field} {raw_name}")
    return result


def _selector_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CarbonDLCConfigError(f"Carbon DLC {field} must be an array")
    if len(value) > _MAX_SELECTORS:
        raise CarbonDLCConfigError(f"Carbon DLC {field} exceeds {_MAX_SELECTORS} selectors")
    return tuple(_clean_selector(item) for item in value)


def _clean_selector(value: object) -> str:
    text = _clean_text(value, field="selector")
    negative = text.startswith("-")
    body = text[1:] if negative else text
    if not body:
        raise CarbonDLCConfigError("Carbon DLC selector cannot be empty")
    if body.startswith("token:"):
        _clean_token(body[6:], field="raw token selector")
    elif body not in {"all", "none"}:
        _clean_identifier(body, field="selector")
    return f"-{body}" if negative else body


def _reject_reserved_selector_name(value: str, *, field: str) -> None:
    if value in {"all", "none"} or value.startswith("token"):
        raise CarbonDLCConfigError(f"Carbon DLC {field} uses reserved name {value!r}")


def _clean_identifier(value: object, *, field: str) -> str:
    text = _clean_text(value, field=field).casefold()
    if not all(character.isalnum() or character in {"_", "-"} for character in text):
        raise CarbonDLCConfigError(
            f"Carbon DLC {field} contains unsupported characters: {value!r}"
        )
    return text


def _clean_token(value: object, *, field: str) -> str:
    text = _clean_text(value, field=field)
    if any(character in text for character in ("\x00", "\r", "\n", "=")):
        raise CarbonDLCConfigError(f"Carbon DLC {field} contains wire delimiter characters")
    return text


def _clean_optional_text(value: object, *, field: str) -> str:
    if value in {None, ""}:
        return ""
    return _clean_text(value, field=field)


def _clean_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CarbonDLCConfigError(f"Carbon DLC {field} must be text")
    text = value.strip()
    if not text:
        raise CarbonDLCConfigError(f"Carbon DLC {field} cannot be empty")
    if len(text) > _MAX_TEXT:
        raise CarbonDLCConfigError(f"Carbon DLC {field} exceeds {_MAX_TEXT} characters")
    return text


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
