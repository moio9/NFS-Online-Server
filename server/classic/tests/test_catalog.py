"""Topology checks for the U2/MW-only package."""

import unittest

from classic.core.catalog import EAService, GameId, PROFILES


class GameCatalogTests(unittest.TestCase):
    def test_both_games_use_the_shared_classic_stack(self) -> None:
        for game in (GameId.MOST_WANTED, GameId.UNDERGROUND2):
            profile = PROFILES[game]
            self.assertTrue(profile.supports(EAService.BOOTSTRAP))
            self.assertTrue(profile.supports(EAService.LOBBY))
            self.assertTrue(profile.supports(EAService.MESSENGER))
            self.assertTrue(profile.supports(EAService.RACE_RELAY))


if __name__ == "__main__":
    unittest.main()
