"""Carbon EA moderator and viral vinyl progression tests."""

from pathlib import Path
import tempfile
import unittest

from carbon.accounts.identity import IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.progression import (
    BEAT_MODERATOR_STAT,
    CARBON_PLAGUE_TOKEN,
    CarbonProgressionStore,
    EA_MODERATOR_ROLE,
    VIRUS_STAT_TO_TOKEN,
)


class CarbonProgressionTests(unittest.TestCase):
    def _identities(self):
        identities = IdentityStore(token_factory=lambda: "key.")
        moderator, _ = identities.login("EA_Mod", "EA_Mod")
        winner, _ = identities.login("Driver", "Driver")
        other, _ = identities.login("Other", "Other")
        return moderator, winner, other

    def test_roles_and_unlocks_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            first = CarbonProgressionStore(path)
            moderator, winner, _other = self._identities()
            first.bind_identity(moderator)
            first.bind_identity(winner)
            first.set_role("EA_Mod", EA_MODERATOR_ROLE, True)
            first.set_stat("EA_Mod", "Virus2", 1.0)

            second = CarbonProgressionStore(path)
            self.assertEqual(second.stat_for_profile(moderator.profile_id, "Moderator"), 1.0)
            self.assertEqual(second.stat_for_profile(moderator.profile_id, "Virus2"), 1.0)

    def test_event_ranking_text_and_minimum_update_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            first = CarbonProgressionStore(path)
            _moderator, winner, _other = self._identities()
            first.bind_identity(winner)
            stat = "Evt_Bst_1229E0FC"
            slower_text = "0x11111111,96.000000,70.000000,0x00000001"
            faster_text = "0x22222222,90.000000,75.000000,0x00000002"
            self.assertTrue(
                first.apply_rank_update(
                    winner.profile_id,
                    stat,
                    100.0,
                    update_type=1,
                    trusted_server=False,
                    text=slower_text,
                )
            )
            self.assertTrue(
                first.apply_rank_update(
                    winner.profile_id,
                    stat,
                    92.0,
                    update_type=1,
                    trusted_server=False,
                    text=faster_text,
                )
            )
            self.assertFalse(
                first.apply_rank_update(
                    winner.profile_id,
                    stat,
                    110.0,
                    update_type=1,
                    trusted_server=False,
                    text="0x33333333,99.000000,60.000000,0x00000003",
                )
            )

            second = CarbonProgressionStore(path)
            self.assertEqual(second.stat_for_profile(winner.profile_id, stat), 92.0)
            self.assertEqual(second.stat_text_for_profile(winner.profile_id, stat), faster_text)

    def test_first_textual_update_rehydrates_an_old_numeric_only_event_row(self) -> None:
        store = CarbonProgressionStore()
        _moderator, winner, _other = self._identities()
        store.bind_identity(winner)
        stat = "Evt_Bst_1229E0FC"
        store.set_stat(winner.account_name, stat, 80.0)

        metadata = "0x11111111,55.000000,70.000000,0x00000001"
        self.assertTrue(
            store.apply_rank_update(
                winner.profile_id,
                stat,
                100.0,
                update_type=1,
                trusted_server=False,
                text=metadata,
            )
        )
        self.assertEqual(store.stat_for_profile(winner.profile_id, stat), 100.0)
        self.assertEqual(store.stat_text_for_profile(winner.profile_id, stat), metadata)

    def test_original_event_mapping_and_beat_moderator_reward(self) -> None:
        store = CarbonProgressionStore()
        moderator, winner, other = self._identities()
        for identity in (moderator, winner, other):
            store.bind_identity(identity)
        store.set_role("EA_Mod", EA_MODERATOR_ROLE, True)
        store.set_stat("EA_Mod", "Virus2", 1.0)

        awards = store.award_race(
            (moderator, winner, other),
            event_type=3,
            winners=(winner.profile_id,),
            ranked=True,
        )
        self.assertEqual(awards.viral_stat, "Virus2")
        self.assertEqual(
            set(awards.viral_recipients),
            {winner.profile_id, other.profile_id},
        )
        self.assertEqual(awards.beat_moderator_recipients, (winner.profile_id,))
        self.assertEqual(store.stat_for_profile(winner.profile_id, BEAT_MODERATOR_STAT), 1.0)

    def test_unranked_race_spreads_virus_without_beat_moderator_reward(self) -> None:
        store = CarbonProgressionStore()
        moderator, winner, other = self._identities()
        for identity in (moderator, winner, other):
            store.bind_identity(identity)
        store.set_role("EA_Mod", EA_MODERATOR_ROLE, True)
        store.set_stat("EA_Mod", "Virus2", 1.0)

        awards = store.award_race(
            (moderator, winner, other),
            event_type=3,
            winners=(winner.profile_id,),
            ranked=False,
        )

        self.assertEqual(awards.viral_stat, "Virus2")
        self.assertEqual(
            set(awards.viral_recipients),
            {winner.profile_id, other.profile_id},
        )
        self.assertEqual(awards.beat_moderator_recipients, ())
        self.assertEqual(store.stat_for_profile(winner.profile_id, BEAT_MODERATOR_STAT), 0.0)

    def test_theater_game_mode_aliases_use_the_original_virus_mapping(self) -> None:
        store = CarbonProgressionStore()
        carrier, receiver, _other = self._identities()
        for identity in (carrier, receiver):
            store.bind_identity(identity)

        cases = (
            (15, "Virus1", "VIRUS_KNOCKOUT_FEVER"),
            (13, "Virus2", "VIRUS_CANYON_CRAZE"),
            (14, "Virus3", "VIRUS_PURSUIT_PANDEMIC"),
        )
        for event_type, stat, token in cases:
            store.set_stat(carrier.account_name, stat, 1.0)
            awards = store.award_race(
                (carrier, receiver),
                event_type=event_type,
                winners=(),
                ranked=False,
            )
            self.assertEqual(awards.viral_stat, stat)
            self.assertIn(receiver.profile_id, awards.viral_recipients)
            self.assertIn(token, store.viral_tokens_for_profile(receiver.profile_id))

    def test_third_virus_derives_carbon_plague_for_dobj(self) -> None:
        store = CarbonProgressionStore()
        carrier, receiver, _other = self._identities()
        for identity in (carrier, receiver):
            store.bind_identity(identity)
        store.set_stat(carrier.account_name, "Virus3", 1.0)
        store.set_stat(receiver.account_name, "Virus1", 1.0)
        store.set_stat(receiver.account_name, "Virus2", 1.0)

        awards = store.award_race(
            (carrier, receiver),
            event_type=14,
            winners=(),
            ranked=False,
        )

        self.assertEqual(awards.viral_recipients, (receiver.profile_id,))
        self.assertEqual(awards.carbon_plague_recipients, (receiver.profile_id,))
        self.assertEqual(
            store.viral_tokens_for_profile(receiver.profile_id),
            (
                VIRUS_STAT_TO_TOKEN["Virus1"],
                VIRUS_STAT_TO_TOKEN["Virus2"],
                VIRUS_STAT_TO_TOKEN["Virus3"],
                CARBON_PLAGUE_TOKEN,
            ),
        )

    def test_trusted_static_dlc_assignment_seeds_a_persistent_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            carrier, _receiver, _other = self._identities()
            first = CarbonProgressionStore(path)
            first.bind_identity(carrier)
            imported = first.import_viral_tokens(
                carrier,
                (
                    "VIRUS_KNOCKOUT_FEVER",
                    "VIRUS_CANYON_CRAZE",
                    "VIRUS_PURSUIT_PANDEMIC",
                    "VIRUS_CARBON_PLAGUE",
                ),
            )
            self.assertEqual(imported, ("Virus1", "Virus2", "Virus3"))

            second = CarbonProgressionStore(path)
            self.assertEqual(
                second.viral_tokens_for_profile(carrier.profile_id),
                (
                    "VIRUS_KNOCKOUT_FEVER",
                    "VIRUS_CANYON_CRAZE",
                    "VIRUS_PURSUIT_PANDEMIC",
                    "VIRUS_CARBON_PLAGUE",
                ),
            )

    def test_client_cannot_self_grant_server_managed_stats(self) -> None:
        store = CarbonProgressionStore()
        _moderator, winner, _other = self._identities()
        store.bind_identity(winner)
        self.assertFalse(
            store.apply_rank_update(
                winner.profile_id,
                "Virus1",
                1.0,
                update_type=0,
                trusted_server=False,
            )
        )
        self.assertFalse(
            store.apply_rank_update(
                winner.profile_id,
                "Moderator",
                1.0,
                update_type=0,
                trusted_server=True,
            )
        )
        self.assertEqual(store.stat_for_profile(winner.profile_id, "Virus1"), 0.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, "Moderator"), 0.0)
        for stat in ("Skill_Level", "Online_Rep", "Total_Online_Games_Started"):
            self.assertFalse(
                store.apply_rank_update(
                    winner.profile_id,
                    stat,
                    9999.0,
                    update_type=0,
                    trusted_server=False,
                )
            )
        self.assertEqual(store.stat_for_profile(winner.profile_id, "Skill_Level"), 1000.0)
        self.assertEqual(store.stat_for_profile(winner.profile_id, "Online_Rep"), 0.0)


class CarbonRankingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore(token_factory=lambda: "rank-key.")
        self.progression = CarbonProgressionStore()
        self.service = CarbonFESLService(
            CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
            self.identities,
            progression=self.progression,
            authentication_mode="open",
        )
        self.connection = FESLConnection()
        self.service.dispatch(
            FESLFrame.from_fields("acct", {"TXN": "Login", "name": "EA_Mod"}, transaction=1),
            self.connection,
        )
        assert self.connection.identity is not None
        self.progression.set_role("EA_Mod", EA_MODERATOR_ROLE, True)
        self.progression.set_stat("EA_Mod", "Virus1", 1.0)

    def _add_driver(self, persona: str, *, skill: float, rep: float = 0.0):
        connection = FESLConnection()
        self.service.dispatch(
            FESLFrame.from_fields("acct", {"TXN": "Login", "name": persona}, transaction=1),
            connection,
        )
        assert connection.identity is not None
        self.progression.set_stat(persona, "Skill_Level", skill)
        self.progression.set_stat(persona, "Online_Rep", rep)
        return connection.identity

    def test_get_stats_for_owners_returns_nested_original_shape(self) -> None:
        identity = self.connection.identity
        assert identity is not None
        request = FESLFrame.from_fields(
            "rank",
            {
                "TXN": "GetStatsForOwners",
                "owners.[]": "1",
                "owners.0.ownerId": str(identity.profile_id),
                "owners.0.ownerType": "1",
                "keys.[]": "3",
                "keys.0": "Virus1",
                "keys.1": "Virus2",
                "keys.2": "Moderator",
            },
            transaction=2,
        )
        reply = self.service.dispatch(request, self.connection)[0]
        self.assertEqual(reply.fields["stats.0.ownerId"], str(identity.profile_id))
        self.assertEqual(reply.fields["stats.0.stats.0.value"], "1.0000")
        self.assertEqual(reply.fields["stats.0.stats.1.value"], "0.0000")
        self.assertEqual(reply.fields["stats.0.stats.2.value"], "1.0000")

    def test_update_stats_requires_dedicated_server_for_virus(self) -> None:
        identity = self.connection.identity
        assert identity is not None
        request_fields = {
            "TXN": "UpdateStats",
            "u.[]": "1",
            "u.0.o": str(identity.profile_id),
            "u.0.ot": "1",
            "u.0.s.[]": "1",
            "u.0.s.0.ut": "0",
            "u.0.s.0.k": "Virus3",
            "u.0.s.0.v": "1.0000",
            "u.0.s.0.t": "",
        }
        self.service.dispatch(FESLFrame.from_fields("rank", request_fields, transaction=3), self.connection)
        self.assertEqual(self.progression.stat_for_profile(identity.profile_id, "Virus3"), 0.0)

        self.connection.dedicated_server = True
        self.service.dispatch(FESLFrame.from_fields("rank", request_fields, transaction=4), self.connection)
        self.assertEqual(self.progression.stat_for_profile(identity.profile_id, "Virus3"), 1.0)

    def test_get_top_n_returns_ordered_profiles_and_real_ranks(self) -> None:
        rival = self._add_driver("Rival", skill=1125.0)
        other = self._add_driver("Other", skill=875.0)

        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "rank",
                {
                    "TXN": "GetTopN",
                    "name": "Skill_Level",
                    "ownerType": "1",
                    "minRank": "1",
                    "maxRank": "10",
                    "periodId": "0",
                    "periodPast": "0",
                },
                transaction=5,
            ),
            self.connection,
        )[0]

        assert self.connection.identity is not None
        self.assertEqual(reply.fields["stats.[]"], "3")
        self.assertEqual(reply.fields["stats.0.owner"], str(rival.profile_id))
        self.assertEqual(reply.fields["stats.0.name"], "Rival")
        self.assertEqual(reply.fields["stats.0.value"], "1125.0000")
        self.assertEqual(reply.fields["stats.0.rank"], "1")
        self.assertEqual(reply.fields["stats.1.owner"], str(self.connection.identity.profile_id))
        self.assertEqual(reply.fields["stats.1.rank"], "2")
        self.assertEqual(reply.fields["stats.2.owner"], str(other.profile_id))
        self.assertEqual(reply.fields["stats.2.rank"], "3")

    def test_carbon_get_top_n_key_returns_event_metadata_and_skips_missing_rows(self) -> None:
        identity = self.connection.identity
        assert identity is not None
        rival = self._add_driver("Rival", skill=1125.0)
        self._add_driver("NoEventTime", skill=875.0)
        stat = "Evt_Bst_1229E0FC"
        own_text = "0x11111111,50.000000,70.000000,0x00000001"
        rival_text = "0x22222222,48.000000,75.000000,0x00000002"
        self.progression.apply_rank_update(
            identity.profile_id,
            stat,
            96.0,
            update_type=1,
            trusted_server=False,
            text=own_text,
        )
        self.progression.apply_rank_update(
            rival.profile_id,
            stat,
            90.0,
            update_type=1,
            trusted_server=False,
            text=rival_text,
        )

        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "rank",
                {
                    "TXN": "GetTopN",
                    "key": stat,
                    "ownerType": "1",
                    "minRank": "1",
                    "maxRank": "10",
                    "periodId": "0",
                    "periodPast": "0",
                },
                transaction=8,
            ),
            self.connection,
        )[0]

        self.assertEqual(reply.fields["key"], stat)
        self.assertEqual(reply.fields["stats.[]"], "2")
        self.assertEqual(reply.fields["stats.0.owner"], str(rival.profile_id))
        self.assertEqual(reply.fields["stats.0.value"], "90.0000")
        self.assertEqual(reply.fields["stats.0.text"], rival_text)
        self.assertEqual(reply.fields["stats.1.owner"], str(identity.profile_id))
        self.assertEqual(reply.fields["stats.1.text"], own_text)

    def test_update_stats_preserves_carbon_event_text(self) -> None:
        identity = self.connection.identity
        assert identity is not None
        stat = "Evt_Bst_1CA676DD"
        metadata = "0xABCDEF01,42.500000,88.250000,0x00000004"
        request = FESLFrame.from_fields(
            "rank",
            {
                "TXN": "UpdateStats",
                "u.[]": "1",
                "u.0.o": str(identity.profile_id),
                "u.0.ot": "1",
                "u.0.s.[]": "1",
                "u.0.s.0.ut": "1",
                "u.0.s.0.k": stat,
                "u.0.s.0.v": "84.1250",
                "u.0.s.0.t": metadata,
            },
            transaction=9,
        )
        self.service.dispatch(request, self.connection)

        self.assertEqual(self.progression.stat_for_profile(identity.profile_id, stat), 84.125)
        self.assertEqual(self.progression.stat_text_for_profile(identity.profile_id, stat), metadata)

    def test_get_top_n_and_stats_returns_client_add_stats_shape(self) -> None:
        rival = self._add_driver("Rival", skill=1125.0, rep=42.0)

        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "rank",
                {
                    "TXN": "GetTopNAndStats",
                    "name": "Skill_Level",
                    "ownerType": "1",
                    "minRank": "1",
                    "maxRank": "1",
                    "periodId": "0",
                    "periodPast": "0",
                    "keys.[]": "2",
                    "keys.0": "Online_Rep_Level",
                    "keys.1": "Virus1",
                },
                transaction=6,
            ),
            self.connection,
        )[0]

        self.assertEqual(reply.fields["stats.[]"], "1")
        self.assertEqual(reply.fields["stats.0.owner"], str(rival.profile_id))
        self.assertEqual(reply.fields["stats.0.addStats.[]"], "2")
        self.assertEqual(reply.fields["stats.0.addStats.0.key"], "Online_Rep_Level")
        self.assertEqual(reply.fields["stats.0.addStats.0.value"], "42.0000")
        self.assertEqual(reply.fields["stats.0.addStats.1.key"], "Virus1")

    def test_get_ranked_stats_for_owners_returns_nested_ranked_shape(self) -> None:
        rival = self._add_driver("Rival", skill=1125.0)
        identity = self.connection.identity
        assert identity is not None

        reply = self.service.dispatch(
            FESLFrame.from_fields(
                "rank",
                {
                    "TXN": "GetRankedStatsForOwners",
                    "owners.[]": "2",
                    "owners.0.ownerId": str(identity.profile_id),
                    "owners.0.ownerType": "1",
                    "owners.1.ownerId": str(rival.profile_id),
                    "owners.1.ownerType": "1",
                    "periodId": "0",
                    "keys.[]": "1",
                    "keys.0": "Skill_Level",
                },
                transaction=7,
            ),
            self.connection,
        )[0]

        self.assertEqual(reply.fields["rankedStats.[]"], "2")
        self.assertEqual(reply.fields["rankedStats.0.ownerId"], str(identity.profile_id))
        self.assertEqual(reply.fields["rankedStats.0.rankedStats.0.value"], "1000.0000")
        self.assertEqual(reply.fields["rankedStats.0.rankedStats.0.rank"], "2")
        self.assertEqual(reply.fields["rankedStats.1.ownerId"], str(rival.profile_id))
        self.assertEqual(reply.fields["rankedStats.1.rankedStats.0.rank"], "1")


if __name__ == "__main__":
    unittest.main()
