# Carbon LadderService evidence and implementation boundary

Inspected local `Games/Need for Speed - Server/NFSC.exe` (PE32), SHA-256
`baaaaec33873d4ba7e4dbae6a5212049ffdcf9679a35d865d7625b4189952fd3`.
This identifies the inspected executable; it does not establish that the local
installation is byte-for-byte unmodified retail media.

Used ASCII strings with file offsets, PE section mappings, absolute-address
cross-references and x86 disassembly (`objdump -d -Mintel`). For the inspected
text/rdata sections, VA = file offset + 0x400000. Objdump warns about a truncated
symbol table but emits the relevant code. No binary is redistributed.

* `0xa1a0f8`: FESL SDK `2.9.0.0/source/ladderservice.cpp` path.
* `0x8f97d0`: initializes ListLadders, TopNLadderParticipants and
  GetParticipantRank transaction strings at `0xa19e20`, `0xa19e08`, `0xa19df4`.
* `0x8fa5e0`: ListLadders serializer, command word `0x6c646472` at `0x8fa609`.
  Participant/ladder arrays, namePrefixMatch, participantState and attr key,
  value, numeric op and type S are serialized here. Knowing that op is numeric
  does not establish which comparison each value means.
* `0x8f98f0`: ladder row parser. Reads id, name, participantType, dates,
  owner/creator IDs and types. Map prefixes `ladders.%d.properties.{` and
  `ladders.%d.searchAttrs.{` are passed to the map parser at `0x93fc70` with a
  closing-brace delimiter. Empty maps have no fabricated entries.
* `0x8f9bf0`: participant parser reads name, rank, participantId, ladderId and
  score. IDs use the two-word reader at `0x8de510`; score is parsed as a float.
  The name lookup terminates iteration when a row is absent. Empty lists
  therefore contain no synthetic participant, score or error-code row.
* `0x8f9d80` / `0x8f9dc0`: alternate participant response parser reads the
  top-level ladderId and indexed participant fields.
* `0x8fa3a0`: GetParticipantRank serializer includes participantId, ladderIds
  array and periodId/periodPast; `0x8fa470`: TopNLadderParticipants serializer
  includes ladderId, minRank/maxRank and the period selectors.
* `0x8fa8b1` onward: date filters use `%02d/%02d/%4d` (`0xa16188`). The date
  object stores month, day, year (month adjustment in `0x8eb130`). The response
  date reader is `0x8eb3d0`, which instead uses `%3s-%d-%d` at
  `0xa17984` and uppercase JAN through DEC. Request filters use month/day/year;
  catalog response dates use e.g. JAN-01-2026. Both are independent of
  rank/GetDateRange.

The ladder-related strings, transaction xrefs, serializers, and service vtable
references inspected did **not** establish a Carbon-specific catalog or any
ladder-to-stat mapping. No direct calls to the two participant request
serializers were found in the disassembly; this alone cannot exclude indirect
calls or externally supplied definitions. Do not conclude the catalog cannot
exist elsewhere. `RETAIL_LADDERS` is deliberately empty until stronger evidence
is available. Tests inject explicitly synthetic definitions through the service
constructor; none are installed as production ladders.

Both participant operations read the same persisted progression identities and
stat values. Only a trusted definition selects a stat and direction. There is no
ladder database, identity creation, enrollment, or write operation. Missing,
malformed and unknown selectors return an empty retail-shaped array rather than
an invented error number. Filters are bounded and validated. Attribute filters
are structurally parsed but fail closed until operator semantics are recovered;
participant-state filters also fail closed because no enrollment state is known.
The serializer writes `namePrefixMatch` as a numeric boolean through
`0x8ddcc0` -> `0x8f48b0` (0/1), and writes the actual search text in `name` at
`0x8fa8a5`. Prefix matching is currently literal and date bounds inclusive; these are
isolated engine policies, not capture-proven matching semantics.

NFSC reads server-assigned ranks and does not resolve ties in these parsers.
The existing rebroadcaster race-result code determines race placements, not
LadderService score ties; no ladder capture was found in this checkout.
`assign_ranks` explicitly retains provisional ordinal ranking, with score,
case-folded persona and profile ID as stable ordering keys. It is shared with
RankingService without changing its behavior. Change that isolated function (or
supply a separate ranker) when ladder tie evidence is available. Filtering and
windowing happen after rank assignment, so they do not renumber results.

The current store has no period snapshots: nonzero period selectors fail closed.
No historic dates or historical scores are fabricated. Future proven period
ladders may require more than a mapping onto a lifetime stat. All catalog dates
and scoring associations in tests are fixtures only. Responses are bounded to
1000 participant rows; requested rank bounds are inclusive.

## Deeper client call-site inspection

* LadderService getter `0x8de950` lazily allocates through `0x8faab0` and
  stores the service in context offset `0x564`. The assertion at `0x8df8f6`
  independently identifies this member as mLadderService. The service vtable
  at `0xa19fd4` has GetParticipantRank at +0x0c, ListLadders at +0x10, and
  TopNLadderParticipants at +0x14. Its absolute references are in the two
  SDK construction paths, `0x8fa383` and `0x8faad3`.
* A whole-disassembly direct call/jump search found no references to
  `0x8de950`; a byte scan for its little-endian absolute address found none
  either. Within the FESL context code, other +0x564 accesses initialize,
  destroy, or route to the service. The receive router at `0x936ee2` checks
  lddr and returns the existing pointer; it does not create a ladder catalog.
* In contrast, RankingService getter `0x8dec00` uses context offset `0x53c`
  and has 27 direct call sites in game code (e.g. `0x818dba`, `0x82cf5b`,
  `0x82cfb0`, `0x833e77`). The caller at `0x82cf5b` then invokes a Ranking
  service virtual method. This is positive evidence of RankingService use.
* Game stat registration at `0x82eb51` associates internal constant 0x42f6
  with Skill_Level (`0x9f5e6c`); `0x82ec7f` associates 0x42fc with
  Online_Rep_Level (`0x9f5e24`). These are internal stat selectors, **not**
  demonstrated ladder IDs. The frontend path near `0x5b49ab` uses 0x42fc
  alongside other stat selectors. They must not be turned into catalog IDs.
* Participant-state serialization at `0x8fa714..0x8fa757` maps SDK enum
  1/2/3/4 to I/A/R/D. This proves the wire letters only; it does not prove
  which persisted Carbon accounts have which ladder membership state.

This strengthens the inference that LadderService is SDK functionality included
but unused by the inspected game's ordinary ranking paths. It is not a proof
that every runtime/indirect path is impossible, nor evidence of an original
server-side catalog. Keep production definitions empty. The concrete corrections
from this pass are the boolean name-prefix filter and distinct response-date
format; fixture tests now exercise both.
