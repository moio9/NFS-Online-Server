"""Carbon's game-session policy for the shared social registry."""
from common.social import GameSessionPolicy, SocialService

CARBON_SESSION_POLICY = GameSessionPolicy(record_recent_players=True, publish_leave=False)


def install_carbon_session_policy(social: SocialService) -> None:
    social.set_game_session_policy("carbon", CARBON_SESSION_POLICY)
