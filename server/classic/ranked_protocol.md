# Classic ranked eligibility

MW executable inspected: SHA-256
`80774c2e5d619b4f120b48d4462896fd504c263399d203a238769cffde1d253c`.

- At 0x78bf10, room options clear/set custom flag 0x200 from option byte
  +0x141. The metadata builder at 0x7904a0 names that option RANKED.
- 0x79b3a0 decodes bit 9 back into that option.
- 0x83acd0 serializes flags with alphabet `@ABCDEFGHIJKLMNOPQRSTUVWXYZ0123-`;
  bit 9 is therefore `I`. `JKM-` alone does not mean ranked.
- Both game creation and owner uadm updates carry CUSTFLAGS. The server also
  retains numeric flag compatibility and owner gset option updates.

At the first ACTIVE transition, the session freezes its participants, room,
and custom flags. Reports require a started, active/finished session and an
authenticated participant present at start. Duplicate reports remain ignored.
Ranked points require at least two participants at start.

U2 retains the existing A.LAN through D.LAN ranked mapping; this change does
not claim to recover a new U2 catalog. The room at start determines eligibility.
MW uses its custom flag at start. Subsequent option changes cannot reclassify
either game's results. Unranked reports can close sessions and enter SQLite
history but do not alter ranking; the JSON fallback likewise leaves stats alone.

Malformed MW RESU cannot fall back to legacy OUTCOME fields. Existing
field-only reports and reply compatibility fields are preserved. Missing MW
reports at owner replacement use the same frozen eligibility.

The existing +100 / -5 / -25 Rep formula is unchanged and is not asserted to be
the original EA formula. Client result authenticity and retail scoring remain separate work;
these checks do not establish a complete
anti-cheat system or a fully reconstructed retail ranked implementation.

## Reconciliation and terminal reports

The lobby now journals each authenticated report with aggregates deferred.
Once every start participant has either reported or has a terminal missing
report, all reports are compared by category, name-mapped placements, and U2
track/direction/laps. Disputed races remain in history without ranked awards.
The whole valid batch commits in one SQLite transaction. Pending journal rows
can only be promoted once; the JSON compatibility backend saves the batch
atomically without introducing another stats database.

The start roster includes names and survives later lobby departures. Ranking
correlation is independent of room membership, so a survivor can report after
the MW owner's socket closed and the room was removed. Explicit game leave or
creation of another game marks that participant as departed. A native report
from the old race supplies the category needed to finalize the missing report
as a disconnect. The existing MW owner-replacement terminal rule is retained.
U2's normal lobby-socket close at race start remains a transport transition,
not a disconnect. Replies for a removed room never republish that room.

No winner/category is invented when all clients disappear without any result
evidence. Pending SQLite reports survive a restart, but live session correlation
is currently in memory; automatic adjudication of those orphaned reports is
not implemented. There is no fabricated timeout penalty or claim of detecting
colluding clients. The earlier formula limitation remains.
