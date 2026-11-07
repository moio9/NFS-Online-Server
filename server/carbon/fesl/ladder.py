"""Read-only retail LadderService engine; catalog evidence: ladder_protocol.md."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Callable

from carbon.accounts.identity import Identity
from carbon.progression import CarbonProgressionStore


Row = tuple[int, Identity, float]


def assign_ranks(rows: list[tuple[Identity, float]]) -> list[Row]:
    """Provisional ordinal ranks, NOT a claim about retail ladder ties.

    NFSC consumes server ranks; available evidence does not establish ties.
    Keep this policy separate from sorting, filtering and wire serialization.
    RankingService retains its existing behavior through this same helper.
    """
    return [(index, identity, score) for index, (identity, score) in enumerate(rows, 1)]


def rank_rows(rows: list[tuple[Identity, float]], *, ascending: bool,
              ranker: Callable[[list[tuple[Identity, float]]], list[Row]] = assign_ranks,
              ) -> list[Row]:
    rows.sort(key=lambda row: (row[1] if ascending else -row[1],
                              row[0].persona.casefold(), row[0].profile_id))
    return ranker(rows)


@dataclass(frozen=True)
class LadderDefinition:
    # All metadata and scoring must be supplied by a trusted catalog, never
    # inferred from a request's ladderId/name or exposed as a write operation.
    id: int
    name: str
    participant_type: str
    start_date: str
    end_date: str
    owner_id: int
    owner_type: str
    creator_id: int
    creator_type: str
    stat: str
    ascending: bool
    properties: tuple[tuple[str, str], ...] = ()
    search_attrs: tuple[tuple[str, str], ...] = ()

    def fields(self, prefix: str) -> dict[str, str]:
        result = {f'{prefix}.{key}': str(value) for key, value in (
            ('id', self.id), ('name', self.name),
            ('participantType', self.participant_type),
            ('startDate', self.start_date), ('endDate', self.end_date),
            ('ownerId', self.owner_id), ('ownerType', self.owner_type),
            ('creatorId', self.creator_id), ('creatorType', self.creator_type),
        )}
        for name, items in (('properties', self.properties), ('searchAttrs', self.search_attrs)):
            for key, value in items:
                result[f'{prefix}.{name}.{{{key}}}'] = value
        return result


# No Carbon-specific definitions/stat mappings were proven in NFSC.exe.
# Do not substitute synthetic Skill/Rep ladders here. Tests inject fixtures.
RETAIL_LADDERS: tuple[LadderDefinition, ...] = ()
OPERATIONS = frozenset(('ListLadders', 'TopNLadderParticipants', 'GetParticipantRank'))


def integer(value: str, *, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError('invalid integer')
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError('integer out of range')
    return number


def ids(fields: dict[str, str], singular: str, plural: str) -> frozenset[int]:
    count = integer(fields.get(f'{plural}.[]', '0'), maximum=256)
    values = {integer(fields[f'{plural}.{i}']) for i in range(count)}
    if singular in fields:
        single = integer(fields[singular])
        # Both constraints apply; conflicting selectors must not broaden a query.
        if values and single not in values:
            raise ValueError('conflicting selectors')
        values = {single}
    return frozenset(values)


def ladder_date(value: str) -> datetime:
    # Request filter format at NFSC 0x8fa904.
    return datetime.strptime(value, '%m/%d/%Y')


def catalog_date(value: str) -> datetime:
    # Response parser 0x8eb3d0 uses %3s-%d-%d and uppercase English months.
    # Keep request/response formats separate and avoid process locale.
    month, day, year = value.split('-', 2)
    months = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
              'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
    return datetime(integer(year), months.index(month) + 1, integer(day))


@dataclass(frozen=True)
class LadderFilters:
    ladders: frozenset[int]
    participants: frozenset[int]
    participant_type: str | None
    participant_state: str | None
    name: str
    name_prefix_match: bool
    minimum: int
    maximum: int
    dates: tuple[tuple[str, datetime], ...]
    attributes: tuple[tuple[str, str, str, str], ...]

    @classmethod
    def parse(cls, fields: dict[str, str]) -> LadderFilters:
        minimum = integer(fields.get('minRank', '1'), minimum=1, maximum=2**31-1)
        maximum = integer(fields.get('maxRank', str(2**31-1)), minimum=minimum, maximum=2**31-1)
        # SDK serializers also carry these period selectors (8fa3f4/8fa49c).
        # The progression store has no historical period snapshots.
        for key in ('periodId', 'periodPast'):
            if key in fields and integer(fields[key]) != 0:
                raise ValueError('unsupported historical period')
        count = integer(fields.get('attr.[]', '0'), maximum=64)
        attrs = tuple(tuple(fields[f'attr.{i}.{key}'] for key in ('key', 'value', 'op', 'type'))
                      for i in range(count))
        dates = tuple((key, ladder_date(fields[key])) for key in (
            'startDateStart', 'startDateEnd', 'endDateStart', 'endDateEnd') if key in fields)
        bounds = dict(dates)
        for root in ('startDate', 'endDate'):
            if root+'Start' in bounds and root+'End' in bounds and bounds[root+'Start'] > bounds[root+'End']:
                raise ValueError('reversed date range')
        return cls(ids(fields, 'ladderId', 'ladderIds'),
                   ids(fields, 'participantId', 'participantIds'),
                   fields.get('participantType'), fields.get('participantState'),
                   fields.get('name', ''),
                   bool(integer(fields.get('namePrefixMatch', '0'), maximum=1)),
                   minimum, maximum, dates, attrs)

    def matches_name(self, name: str) -> bool:
        if not self.name:
            return True
        return name.startswith(self.name) if self.name_prefix_match else name == self.name

    def matches(self, ladder: LadderDefinition) -> bool:
        if self.ladders and ladder.id not in self.ladders:
            return False
        if self.participant_type is not None and self.participant_type != ladder.participant_type:
            return False
        # Parse but fail closed: neither attribute operator semantics nor
        # persistent ladder membership states have been established. Never
        # silently ignore these constraints or invent enrollment state.
        if self.attributes or self.participant_state is not None:
            return False
        for key, bound in self.dates:
            value = ladder.start_date if key.startswith('startDate') else ladder.end_date
            if not value:
                return False
            actual = catalog_date(value)
            if (key.endswith('Start') and actual < bound) or (key.endswith('End') and actual > bound):
                return False
        return True


class LadderService:
    def __init__(self, progression: CarbonProgressionStore,
                 catalog: tuple[LadderDefinition, ...] = RETAIL_LADDERS) -> None:
        self.progression = progression
        self.catalog = tuple(sorted(catalog, key=lambda item: item.id))
        if len({item.id for item in self.catalog}) != len(self.catalog):
            raise ValueError('duplicate ladder id')

    def _rows(self, ladder: LadderDefinition) -> list[Row]:
        rows = [(identity, score) for identity, score in self.progression.ranking_rows(ladder.stat)
                if math.isfinite(score)]
        return rank_rows(rows, ascending=ladder.ascending)

    def reply(self, fields: dict[str, str]) -> dict[str, str] | None:
        operation = fields.get('TXN', '')
        if operation not in OPERATIONS:
            return None
        root = 'ladders' if operation == 'ListLadders' else 'participants'
        result = {'TXN': operation, f'{root}.[]': '0'}
        try:
            filters = LadderFilters.parse(fields)
            if operation == 'TopNLadderParticipants' and 'ladderId' not in fields:
                return result
            if operation == 'GetParticipantRank' and (not filters.ladders or not filters.participants):
                return result
            if operation == 'TopNLadderParticipants':
                # NFSC 0x8f9d80 reads this before indexed participants.
                result['ladderId'] = str(integer(fields['ladderId']))
            ladders = [item for item in self.catalog if filters.matches(item)]
            if operation == 'ListLadders':
                ladders = [item for item in ladders if filters.matches_name(item.name)
                           and (not filters.participants or any(
                               identity.profile_id in filters.participants
                               for _, identity, _ in self._rows(item)))]
                for index, ladder in enumerate(ladders):
                    result.update(ladder.fields(f'ladders.{index}'))
                result['ladders.[]'] = str(len(ladders))
            else:
                entries = []
                for ladder in ladders:
                    for rank, identity, score in self._rows(ladder):
                        if not filters.minimum <= rank <= filters.maximum:
                            continue
                        if filters.participants and identity.profile_id not in filters.participants:
                            continue
                        if not filters.matches_name(identity.persona):
                            continue
                        entries.append((ladder.id, rank, identity, score))
                # Bound response size without changing global ranks.
                for index, (ladder_id, rank, identity, score) in enumerate(entries[:1000]):
                    prefix = f'participants.{index}'
                    result.update({f'{prefix}.name': identity.persona,
                                   f'{prefix}.rank': str(rank),
                                   f'{prefix}.participantId': str(identity.profile_id),
                                   f'{prefix}.ladderId': str(ladder_id),
                                   f'{prefix}.score': format(score, '.9g')})
                result['participants.[]'] = str(min(1000, len(entries)))
        except (ValueError, KeyError, OverflowError):
            return {'TXN': operation, f'{root}.[]': '0'}
        return result
