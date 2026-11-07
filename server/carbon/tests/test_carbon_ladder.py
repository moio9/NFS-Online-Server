"""Synthetic catalog fixtures test the engine, not retail catalog authenticity."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from carbon.accounts.identity import IdentityStore
from carbon.fesl.frame import FESLFrame, FESLStreamDecoder
from carbon.fesl.ladder import LadderDefinition, LadderFilters, RETAIL_LADDERS
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.progression import CarbonProgressionStore

FIXTURE = LadderDefinition(987654321, 'TEST ONLY', '1', 'JAN-01-2026', 'DEC-31-2026',
                           0, '1', 0, '1', 'Online_Rep', False,
                           (('fixture', 'true'),), (('fixture', 'true'),))


class LadderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'progression.json'
        self.store = CarbonProgressionStore(self.path)
        self.identities = IdentityStore()
        self.players = {}
        for name, score in [('Zed', 10), ('Beta', 30), ('Alpha', 30), ('Last', 5)]:
            identity, _ = self.identities.login(name, name)
            self.players[name] = identity
            self.store.bind_identity(identity)
            self.store.set_stat(name, FIXTURE.stat, score)
        self.service = self.make_service()
        self.token = 0

    def make_service(self, catalog=(FIXTURE,)):
        return CarbonFESLService(CarbonEndpoints('localhost', 1, 'localhost'),
                                 self.identities, progression=self.store, ladder_catalog=catalog)

    def request(self, operation, fields=None, *, command='lddr', service=None):
        self.token += 1
        frame = FESLFrame.from_fields(command, {'TXN': operation, **(fields or {})},
                                     transaction=0xA0000000 | self.token)
        frame = FESLStreamDecoder().feed(frame.encode())[0]
        replies = (service or self.service).dispatch(frame, FESLConnection())
        self.assertEqual(len(replies), 1)
        reply = FESLStreamDecoder().feed(replies[0].encode())[0]
        self.assertEqual(reply.command, command)
        self.assertEqual(reply.transaction, 0x80000000 | self.token)
        self.assertEqual(reply.fields['TXN'], operation)
        return reply.fields

    def top(self, **fields):
        return self.request('TopNLadderParticipants', {'ladderId': str(FIXTURE.id), **fields})

    def test_list_metadata(self):
        self.assertEqual(self.request('ListLadders'), {
            'TXN': 'ListLadders', 'ladders.[]': '1', **FIXTURE.fields('ladders.0')})

    def test_response_dates_differ_from_request_dates(self):
        result = self.request('ListLadders', {'startDateStart': '01/01/2026',
                                               'endDateEnd': '12/31/2026'})
        self.assertEqual(result['ladders.0.startDate'], 'JAN-01-2026')
        self.assertEqual(result['ladders.0.endDate'], 'DEC-31-2026')
        self.assertEqual(self.request('ListLadders', {
            'startDateStart': 'JAN-01-2026'})['ladders.[]'], '0')

    def test_empty_production_catalog(self):
        self.assertEqual(RETAIL_LADDERS, ())
        for operation, root in [('ListLadders', 'ladders'),
                                ('TopNLadderParticipants', 'participants'),
                                ('GetParticipantRank', 'participants')]:
            self.assertEqual(self.request(operation, service=self.make_service(())),
                             {'TXN': operation, f'{root}.[]': '0'})

    def test_list_filters(self):
        for filters, count in [
            ({'ladderIds.[]': '1', 'ladderIds.0': str(FIXTURE.id)}, '1'),
            ({'ladderId': '9'}, '0'), ({'participantType': '1'}, '1'),
            ({'participantType': '2'}, '0'), ({'name': 'TEST', 'namePrefixMatch': '1'}, '1'),
            ({'name': 'NO', 'namePrefixMatch': '1'}, '0'),
            ({'name': 'TEST', 'namePrefixMatch': '0'}, '0'),
            ({'name': 'TEST ONLY', 'namePrefixMatch': '0'}, '1'),
            ({'participantId': str(self.players['Alpha'].profile_id)}, '1'),
            ({'participantIds.[]': '1', 'participantIds.0': '9'}, '0'),
            ({'startDateStart': '01/01/2026', 'startDateEnd': '01/02/2026',
              'endDateStart': '12/30/2026', 'endDateEnd': '12/31/2026'}, '1'),
            ({'endDateEnd': '12/30/2026'}, '0'),
        ]:
            with self.subTest(filters=filters):
                self.assertEqual(self.request('ListLadders', filters)['ladders.[]'], count)

    def test_invalid_filters_are_empty(self):
        for fields in [
            {'namePrefixMatch': 'TEST'}, {'namePrefixMatch': '2'},
            {'ladderId': '-1'}, {'ladderId': 'abc'}, {'ladderId': str(2**64)},
            {'ladderIds.[]': '257'}, {'ladderIds.[]': '1'},
            {'participantIds.[]': '-1'}, {'participantIds.[]': '1'},
            {'minRank': 'NaN'}, {'minRank': '0'}, {'minRank': '5', 'maxRank': '2'},
            {'startDateStart': 'yesterday'},
            {'startDateStart': '12/31/2026', 'startDateEnd': '01/01/2026'},
            {'attr.[]': '65'}, {'attr.[]': '1'},
            {'ladderId': str(FIXTURE.id), 'ladderIds.[]': '1', 'ladderIds.0': '9'},
        ]:
            with self.subTest(fields=fields):
                self.assertEqual(self.request('ListLadders', fields)['ladders.[]'], '0')

    def test_unproven_attribute_and_state_semantics_fail_closed(self):
        attrs = {'attr.[]': '1', 'attr.0.key': 'fixture', 'attr.0.value': 'true',
                 'attr.0.op': '0', 'attr.0.type': 'S'}
        self.assertEqual(LadderFilters.parse(attrs).attributes, (('fixture', 'true', '0', 'S'),))
        for fields in [attrs, {'participantState': 'A'}, {'participantState': 'I'}]:
            self.assertEqual(self.request('ListLadders', fields)['ladders.[]'], '0')
            self.assertEqual(self.top(**fields)['participants.[]'], '0')

    def test_ordering_multiple_participants_and_provisional_ties(self):
        result = self.top()
        self.assertEqual(result['participants.[]'], '4')
        self.assertEqual(result['ladderId'], str(FIXTURE.id))
        for i, (name, score) in enumerate([('Alpha', '30'), ('Beta', '30'), ('Zed', '10'), ('Last', '5')]):
            prefix = f'participants.{i}'
            self.assertEqual(result[f'{prefix}.name'], name)
            self.assertEqual(result[f'{prefix}.score'], score)
            self.assertEqual(result[f'{prefix}.rank'], str(i+1))
            self.assertEqual(result[f'{prefix}.participantId'], str(self.players[name].profile_id))
            self.assertEqual(result[f'{prefix}.ladderId'], str(FIXTURE.id))
        self.assertEqual(result, self.top())

    def test_rank_window_is_inclusive_and_does_not_renumber(self):
        result = self.top(minRank='2', maxRank='3')
        self.assertEqual(result['participants.[]'], '2')
        self.assertEqual(result['participants.0.name'], 'Beta')
        self.assertEqual(result['participants.0.rank'], '2')
        self.assertEqual(result['participants.1.rank'], '3')
        self.assertEqual(self.top(minRank='100')['participants.[]'], '0')

    def test_participant_filters_preserve_global_rank(self):
        result = self.top(participantId=str(self.players['Zed'].profile_id))
        self.assertEqual(result['participants.[]'], '1')
        self.assertEqual(result['participants.0.rank'], '3')
        self.assertEqual(self.top(name='Z', namePrefixMatch='1')['participants.0.rank'], '3')

    def test_get_participant_rank_uses_same_view(self):
        for name in self.players:
            fields = {'ladderId': str(FIXTURE.id), 'participantId': str(self.players[name].profile_id)}
            ranked = self.request('GetParticipantRank', fields)
            top = self.request('TopNLadderParticipants', fields)
            ranked.pop('TXN')
            top.pop('TXN')
            top.pop('ladderId')
            self.assertEqual(ranked, top)
        result = self.request('GetParticipantRank', {
            'ladderIds.[]': '1', 'ladderIds.0': str(FIXTURE.id),
            'participantId': str(self.players['Zed'].profile_id)})
        self.assertEqual(result['participants.0.rank'], '3')

    def test_unknown_participant_ladder_and_missing_selectors(self):
        for operation in ['TopNLadderParticipants', 'GetParticipantRank']:
            for fields in [{}, {'ladderId': '9', 'participantId': '9'},
                           {'ladderId': str(FIXTURE.id), 'participantId': '9'}]:
                self.assertEqual(self.request(operation, fields)['participants.[]'], '0')

    def test_reload_and_reads_do_not_write(self):
        before = self.path.read_bytes()
        expected = self.top()
        self.request('ListLadders')
        self.request('GetParticipantRank', {'ladderId': str(FIXTURE.id), 'participantId': '9'})
        self.assertEqual(before, self.path.read_bytes())
        self.store = CarbonProgressionStore(self.path)
        self.service = self.make_service()
        self.assertEqual(expected, self.top())

    def test_ascending_and_nonfinite_scores(self):
        self.service = self.make_service((replace(FIXTURE, ascending=True),))
        self.assertEqual(self.top()['participants.0.name'], 'Last')
        self.store.set_stat('Last', FIXTURE.stat, float('nan'))
        self.assertEqual(self.top()['participants.[]'], '3')

    def test_periods_do_not_fabricate_historical_scores(self):
        for fields in [{'periodId': '1'}, {'periodPast': '1'}, {'periodPast': '-1'}]:
            self.assertEqual(self.top(**fields)['participants.[]'], '0')
        self.assertEqual(self.top(periodId='0', periodPast='0')['participants.[]'], '4')

    def test_transaction_duplicates_and_mutations(self):
        connection = FESLConnection()
        for token, expected in [(0, 0x80000000), (0xA0000042, 0x80000042), (0x80000043, 0x80000043)]:
            frame = FESLFrame.from_fields('lddr', {'TXN': 'ListLadders'}, transaction=token)
            self.assertEqual(self.service.dispatch(frame, connection)[0].transaction, expected)
            self.assertEqual(self.service.dispatch(frame, connection), [])
        rank_frame = FESLFrame.from_fields('rank', {'TXN': 'GetStats'}, transaction=0xA0000042)
        self.assertEqual(len(self.service.dispatch(rank_frame, connection)), 1)
        before = self.path.read_bytes()
        for operation in ['UpdateStats', 'JoinLadder', 'SetParticipantScore']:
            frame = FESLFrame.from_fields('lddr', {'TXN': operation,
                                                 'participantId': str(self.players['Alpha'].profile_id),
                                                 'score': '999999'})
            self.assertEqual(self.service.dispatch(frame, FESLConnection()), [])
        self.assertEqual(before, self.path.read_bytes())

    def test_carbon_ranking_and_ladder_operation_coverage(self):
        for operation in ['GetStats', 'GetStatsForOwners', 'GetRankedStats',
                          'GetRankedStatsForOwners', 'GetTopN', 'GetTopNAndStats',
                          'GetDateRange', 'UpdateStats']:
            with self.subTest(operation=operation):
                self.request(operation, command='rank')
        for operation in ['ListLadders', 'TopNLadderParticipants', 'GetParticipantRank']:
            with self.subTest(operation=operation):
                self.request(operation)
