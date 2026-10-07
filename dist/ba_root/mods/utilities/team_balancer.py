""" balances team players if needed."""
import json

from bascenev1 import broadcastmessage, get_foreground_host_session
from bascenev1._dualteamsession import DualTeamSession


def _team_name(team) -> str:
    """returns a readable team name, for both Lstr and plain str names."""
    name = team.name
    if hasattr(name, "as_json"):
        try:
            return json.loads(name.as_json())["t"][1]
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return str(name)


def _move(source, target) -> None:
    """moves a player from the source team to the target team."""
    player = source.players.pop()
    target.players.append(player)
    player.setdata(target, player.character, target.color, player.highlight)
    icon_info = player.get_icon_info()
    player.set_icon_info(
        icon_info["texture"],
        icon_info["tint_texture"],
        target.color,
        player.highlight,
    )
    broadcastmessage(f"shifted {player.getname()} to {_team_name(target)}")


def check_team_balance():
    """ checks if team are balanced and balances them if needed."""
    from tournament.session import TournamentSession

    session = get_foreground_host_session()
    # only the dual-team sessions are balanced, tournament teams are fixed.
    if not isinstance(session, DualTeamSession) or isinstance(
        session, TournamentSession
    ):
        return

    teams = session.sessionteams
    if len(teams) != 2:
        return

    diff = len(teams[0].players) - len(teams[1].players)
    if diff % 2 != 0:
        # odd number of players, balance is impossible.
        return

    moves = abs(diff) // 2
    source, target = (teams[0], teams[1]) if diff > 0 else (teams[1], teams[0])
    for _ in range(moves):
        _move(source, target)