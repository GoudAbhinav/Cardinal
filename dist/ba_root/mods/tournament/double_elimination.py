"""double elimination: every team is knocked out by its second loss.

the bracket is built whole at the start. a slot that waits for another match knows where its team
comes from (its "source"), so when a match ends, the winner and the loser are moved on to the slots
that wait for them. this also handles any number of teams: the bracket is sized to the next power of
two and the empty places are byes, a match with a bye is decided on its own.

    winners bracket   everyone starts here, a loss drops the team into the losers bracket
    losers bracket    a loss here knocks the team out
    grand final       winners champion vs losers champion. the winners champion has lost nothing,
                      so if the losers champion wins, a second match (the reset) decides it.
"""

from __future__ import annotations

import random

from server.enums import Status
from tournament.schema import (
    DoubleEliminationSchema,
    MatchSchema,
    MatchTeamSchema,
    RoundSchema,
)

WINNERS = "winners"
LOSERS = "losers"
GRAND_FINAL = "grand-final"

BRACKET_ORDER = {WINNERS: 0, LOSERS: 1, GRAND_FINAL: 2}
MINIMUM_TEAMS = 4


def source(kind: str, bracket: str, round_id: int, match_id: int) -> str:
    """kind is "W" (winner of that match) or "L" (loser of it)."""
    return f"{kind}:{bracket}:{round_id}:{match_id}"


def parse_source(text: str) -> tuple[str, str, str, str]:
    kind, bracket, round_id, match_id = text.split(":")
    return kind, bracket, round_id, match_id


def seed_order(size: int) -> list[int]:
    """the seeds in bracket order (1 v size, 2 v size-1 ... spread out so the top seeds meet late)."""
    if size == 1:
        return [1]
    order = []
    for seed in seed_order(size // 2):
        order += [seed, size + 1 - seed]
    return order


def _match(bracket: str, round_id: int, match_id: int, first: MatchTeamSchema, second: MatchTeamSchema) -> MatchSchema:
    return MatchSchema(
        match_id=str(match_id),
        teams=[first, second],
        bracket=bracket,
        round_id=str(round_id),
    )


def _slot(idx: int, origin: str | None = None, name: str | None = None, bye: bool = False) -> MatchTeamSchema:
    return MatchTeamSchema(idx=idx, name=name, source=origin, bye=bye)


def build(teams: list[str], rng: random.Random | None = None) -> DoubleEliminationSchema:
    """makes the whole bracket for the teams (the seeding is random), and settles the byes."""
    if len(teams) < MINIMUM_TEAMS:
        raise ValueError(f"Double elimination needs at least {MINIMUM_TEAMS} teams.")
    rng = rng or random.Random()

    size = 1 << (len(teams) - 1).bit_length()  # the next power of two
    rounds = size.bit_length() - 1

    seeded = list(teams)
    rng.shuffle(seeded)

    def team_at(seed: int) -> MatchTeamSchema | None:
        return None if seed > len(seeded) else seeded[seed - 1]

    order = seed_order(size)

    # ---- winners bracket ----
    winners: dict[str, RoundSchema] = {}
    first_round = {}
    for match_id in range(1, size // 2 + 1):
        slots = []
        for idx, seed in enumerate(order[2 * match_id - 2 : 2 * match_id]):
            name = team_at(seed)
            slots.append(_slot(idx, name=name, bye=name is None))
        first_round[str(match_id)] = _match(WINNERS, 1, match_id, *slots)
    winners["1"] = RoundSchema(matches=first_round)

    for round_id in range(2, rounds + 1):
        matches = {}
        for match_id in range(1, size // 2**round_id + 1):
            matches[str(match_id)] = _match(
                WINNERS,
                round_id,
                match_id,
                _slot(0, source("W", WINNERS, round_id - 1, 2 * match_id - 1)),
                _slot(1, source("W", WINNERS, round_id - 1, 2 * match_id)),
            )
        winners[str(round_id)] = RoundSchema(matches=matches)

    # ---- losers bracket ----
    losers: dict[str, RoundSchema] = {}
    for round_id in range(1, 2 * (rounds - 1) + 1):
        matches = {}
        if round_id == 1:
            # the losers of the first winners round play each other.
            count = size // 4
            for match_id in range(1, count + 1):
                matches[str(match_id)] = _match(
                    LOSERS,
                    round_id,
                    match_id,
                    _slot(0, source("L", WINNERS, 1, 2 * match_id - 1)),
                    _slot(1, source("L", WINNERS, 1, 2 * match_id)),
                )
        elif round_id % 2 == 0:
            # the survivors meet the teams that just dropped from the winners bracket.
            dropping = round_id // 2 + 1  # the winners round the drop comes from
            count = size >> dropping
            for match_id in range(1, count + 1):
                # alternate the order of the drops, so a team does not meet who it just played.
                dropped = count - match_id + 1 if (round_id // 2) % 2 == 1 else match_id
                matches[str(match_id)] = _match(
                    LOSERS,
                    round_id,
                    match_id,
                    _slot(0, source("W", LOSERS, round_id - 1, match_id)),
                    _slot(1, source("L", WINNERS, dropping, dropped)),
                )
        else:
            # the survivors play each other.
            count = len(losers[str(round_id - 1)].matches) // 2
            for match_id in range(1, count + 1):
                matches[str(match_id)] = _match(
                    LOSERS,
                    round_id,
                    match_id,
                    _slot(0, source("W", LOSERS, round_id - 1, 2 * match_id - 1)),
                    _slot(1, source("W", LOSERS, round_id - 1, 2 * match_id)),
                )
        losers[str(round_id)] = RoundSchema(matches=matches)

    # ---- grand final ----
    grand_final = RoundSchema(
        matches={
            "1": _match(
                GRAND_FINAL,
                1,
                1,
                _slot(0, source("W", WINNERS, rounds, 1)),
                _slot(1, source("W", LOSERS, 2 * (rounds - 1), 1)),
            )
        }
    )

    bracket = DoubleEliminationSchema(
        winners=winners,
        losers=losers,
        grand_final=grand_final,
        status=Status.IN_PROGRESS,
    )
    settle(bracket)
    return bracket


# ---- reading the bracket ----


def all_matches(bracket: DoubleEliminationSchema) -> list[MatchSchema]:
    """every match, winners first, then losers, then the grand final."""
    matches = []
    for part in (bracket.winners, bracket.losers, {"1": bracket.grand_final}):
        for round_id in sorted(part, key=int):
            for match_id in sorted(part[round_id].matches, key=int):
                matches.append(part[round_id].matches[match_id])
    return matches


def match_key(match: MatchSchema) -> str:
    return f"{match.bracket}-{match.round_id}-{match.match_id}"


def find(bracket: DoubleEliminationSchema, bracket_name: str, round_id: str, match_id: str) -> MatchSchema:
    part = {WINNERS: bracket.winners, LOSERS: bracket.losers}.get(bracket_name)
    if bracket_name == GRAND_FINAL:
        return bracket.grand_final.matches[match_id]
    return part[round_id].matches[match_id]


def _outcome(match: MatchSchema, kind: str) -> MatchTeamSchema:
    """the slot that won (W) or lost (L) a completed match."""
    return match.teams[match.winner_idx if kind == "W" else match.loser_idx]


def is_known(slot: MatchTeamSchema) -> bool:
    """the slot has a team, or is known to stay empty (a bye)."""
    return slot.name is not None or slot.bye


# ---- moving teams along ----


def settle(bracket: DoubleEliminationSchema) -> None:
    """moves the results of the completed matches into the slots waiting for them, decides the
    matches that have a bye, and handles the grand final (the reset and the end)."""
    matches = all_matches(bracket)
    index = {match_key(match): match for match in matches}

    changed = True
    while changed:
        changed = False
        for match in matches:
            if match.status == Status.COMPLETED:
                continue
            for slot in match.teams:
                if is_known(slot) or slot.source is None:
                    continue
                kind, bracket_name, round_id, match_id = parse_source(slot.source)
                origin = index[f"{bracket_name}-{round_id}-{match_id}"]
                if origin.status != Status.COMPLETED:
                    continue
                taken = _outcome(origin, kind)
                slot.name, slot.bye = taken.name, taken.bye
                changed = True

            if all(is_known(slot) for slot in match.teams) and any(slot.bye for slot in match.teams):
                # a bye decides the match, the other team goes on (two byes make a bye).
                bye_slots = [slot for slot in match.teams if slot.bye]
                match.winner_idx = 1 - bye_slots[0].idx if len(bye_slots) == 1 else 0
                match.loser_idx = 1 - match.winner_idx
                match.status = Status.COMPLETED
                match.is_bye = True
                changed = True

    _settle_grand_final(bracket)


def _settle_grand_final(bracket: DoubleEliminationSchema) -> None:
    matches = bracket.grand_final.matches
    first = matches["1"]
    if first.status != Status.COMPLETED:
        return

    if first.winner_idx == 1 and "2" not in matches:
        # the losers champion won, both teams have lost once now. they play again.
        matches["2"] = _match(
            GRAND_FINAL,
            1,
            2,
            _slot(0, name=first.teams[0].name),
            _slot(1, name=first.teams[1].name),
        )
        return

    decider = matches["2"] if "2" in matches else first
    if decider.status == Status.COMPLETED and bracket.status != Status.COMPLETED:
        bracket.status = Status.COMPLETED
        bracket.grand_final.status = Status.COMPLETED
        bracket.placements = placements(bracket)


def placements(bracket: DoubleEliminationSchema) -> list[dict]:
    """final ranks, by the order the teams were knocked out. teams knocked out in the same round
    share a rank (so there is no third place match: the team that lost the losers final is third)."""
    matches = bracket.grand_final.matches
    decider = matches["2"] if "2" in matches else matches["1"]
    champion = decider.teams[decider.winner_idx].name
    runner_up = decider.teams[decider.loser_idx].name
    result = [{"rank": 1, "team": champion}, {"rank": 2, "team": runner_up}]

    # the losers bracket: the later the round a team lost in, the better its rank.
    by_round: dict[int, list[str]] = {}
    for round_id, round_data in bracket.losers.items():
        for match in round_data.matches.values():
            if match.is_bye:
                continue
            by_round.setdefault(int(round_id), []).append(match.teams[match.loser_idx].name)

    rank = 3
    for round_id in sorted(by_round, reverse=True):
        for team in by_round[round_id]:
            result.append({"rank": rank, "team": team})
        rank += len(by_round[round_id])
    return result


# ---- what can be played ----


def depth(bracket: DoubleEliminationSchema) -> dict[str, int]:
    """how many matches deep each match is, matches that feed it are shallower."""
    matches = all_matches(bracket)
    index = {match_key(match): match for match in matches}
    result: dict[str, int] = {}

    def of(match: MatchSchema) -> int:
        key = match_key(match)
        if key not in result:
            feeders = []
            for slot in match.teams:
                if slot.source:
                    _, bracket_name, round_id, match_id = parse_source(slot.source)
                    feeders.append(of(index[f"{bracket_name}-{round_id}-{match_id}"]))
            if not feeders and match.bracket == GRAND_FINAL and match.match_id == "2":
                feeders = [of(index["grand-final-1-1"])]
            result[key] = 1 + max(feeders, default=0)
        return result[key]

    for match in matches:
        of(match)
    return result


def playable(bracket: DoubleEliminationSchema) -> list[MatchSchema]:
    """the matches of the teams that are known (played, or waiting to be), without the bye matches,
    in a steady order: a match always comes after the ones that feed it."""
    depths = depth(bracket)
    found = [
        match
        for match in all_matches(bracket)
        if not match.is_bye and all(slot.name is not None for slot in match.teams)
    ]
    found.sort(
        key=lambda m: (
            depths[match_key(m)],
            BRACKET_ORDER[m.bracket],
            int(m.round_id),
            int(m.match_id),
        )
    )
    return found
