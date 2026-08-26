"""Topology checks: no game adapter may accidentally enable another's stack."""

import unittest

from carbon.core.catalog import EAService, GameId, PROFILES


class GameCatalogTests(unittest.TestCase):
    def test_carbon_owns_its_specialized_stack(self) -> None:
        profile = PROFILES[GameId.CARBON]
        self.assertTrue(profile.supports(EAService.REBROADCASTER))
        self.assertTrue(profile.supports(EAService.GAME_MANAGER))
        self.assertTrue(profile.supports(EAService.THEATER))
        self.assertTrue(profile.supports(EAService.FESL))
        self.assertFalse(profile.supports(EAService.BOOTSTRAP))
        self.assertFalse(profile.supports(EAService.LOBBY))

    def test_classic_adapters_do_not_enter_carbon_game_manager(self) -> None:
        for game in (GameId.MOST_WANTED, GameId.UNDERGROUND2):
            profile = PROFILES[game]
            self.assertTrue(profile.supports(EAService.BOOTSTRAP))
            self.assertFalse(profile.supports(EAService.REBROADCASTER))
            self.assertFalse(profile.supports(EAService.GAME_MANAGER))
