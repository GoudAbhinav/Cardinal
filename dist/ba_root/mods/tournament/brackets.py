"""storage for brackets"""

import re
from pathlib import Path

from server.enums import Status
from server.storage import Storage
from tournament.storage import SEASONS_DIR
from tournament.registration import Registration
from tournament.graphics import runner
from tournament.schema import BracketsMetaSchema, MatchSchema, MatchTeamSchema, GroupStageSchema, GroupSchema, RoundSchema, TeamSchema

class Brackets(Storage):
    """generates brackets and handles the rounds."""

    def __init__(self, season_id: str):
        super().__init__("brackets.json", SEASONS_DIR / season_id)
        self.season_id = season_id
        self.group_stage_path = self.directory / "rounds" / "group-stage.json"
        self.group_stage_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate_ids()
        self.bootstrap()

    def bootstrap(self):
        """creates the file and setups up method"""
        self.registration = Registration(self.season_id)
        if not self.path.exists():
            self.commit_meta(BracketsMetaSchema())

    # ---- ids ----
    # groups, rounds and matches are named "1", "2", ... (older seasons used "group-1", "round 1",
    # "m1", "match-1", "FINALS", "THIRD_PLACE"), this converts a season that still has the old names.

    LEGACY_NUMBER = re.compile(r"^(?:group|round|match|m)?[-_ ]?(\d+)$", re.IGNORECASE)
    LEGACY_FINALS = {"FINALS": "1", "THIRD_PLACE": "2"}

    @classmethod
    def normalize_id(cls, value):
        """returns the numeric name of an id, ids that are already fine are returned as they are."""
        if not isinstance(value, str):
            return value
        if value in cls.LEGACY_FINALS:
            return cls.LEGACY_FINALS[value]
        found = cls.LEGACY_NUMBER.match(value)
        return found.group(1) if found else value

    def migrate_ids(self) -> None:
        """converts the files of this season to the numeric ids, if they are not already."""
        rounds_dir = self.directory / "rounds"
        for path in rounds_dir.glob("*.json"):
            data = self.read(path)
            if not data:
                continue
            if path.name == "group-stage.json":
                changed = self._migrate_group_stage(data)
            else:
                changed = self._migrate_matches(data.get("matches"))
            if changed:
                self.commit(data, path)

    def _migrate_matches(self, matches: dict | None) -> bool:
        """renames the matches (and their ids) of a round in place."""
        if not isinstance(matches, dict):
            return False
        changed = False
        for key in list(matches):
            match = matches[key]
            new_key = self.normalize_id(key)
            for field in ("match_id", "round_id", "group_id"):
                if isinstance(match, dict) and match.get(field) is not None:
                    new_value = self.normalize_id(match[field])
                    if new_value != match[field]:
                        match[field] = new_value
                        changed = True
            if new_key != key:
                matches[new_key] = matches.pop(key)
                changed = True
        return changed

    def _migrate_group_stage(self, data: dict) -> bool:
        changed = False
        groups = data.get("groups", {})
        for group_key in list(groups):
            group = groups[group_key]
            rounds = group.get("rounds", {})
            for round_key in list(rounds):
                changed |= self._migrate_matches(rounds[round_key].get("matches"))
                new_round_key = self.normalize_id(round_key)
                if new_round_key != round_key:
                    rounds[new_round_key] = rounds.pop(round_key)
                    changed = True
            new_group_key = self.normalize_id(group_key)
            if new_group_key != group_key:
                groups[new_group_key] = groups.pop(group_key)
                changed = True
        return changed

    def generate_group_stage(self, teams: list) -> None | bool:
        """generates the group stage brackets."""
        # TODO: make it more dynamically syncing with real logic.
        # we assume that the total number of teams is even.
        teams_count = len(teams)
        groups_count = (teams_count & -teams_count)
        if teams_count < groups_count * 5:
            groups_count = groups_count // 2

        # ah we dont want the groups to be only two, round-robin will make it longer otherwise or the teams count is less than 4
        assert groups_count >= 4
        if groups_count == teams_count:
            # the number is power of 2 value
            # we go straight to the main stage
            self.generate_main_stage(teams=teams)
            return

        # mhm.. groups are needed now.
        groups = {}
        main_stage_capacity = 1 << (teams_count.bit_length() - 1)
        teams_per_group = teams_count // groups_count

        # calculates the number of winning teams needed out of each group
        winning_teams_per_group = main_stage_capacity // groups_count
        if winning_teams_per_group * 2 - 1 > teams_per_group:
            winning_teams_per_group = winning_teams_per_group // 2

        for index in range(groups_count):
            group_id = str(index + 1)
            groups[group_id] = GroupSchema(
                rounds = self.generate_round_robin(
                    teams[index * teams_per_group : (index + 1) * teams_per_group],
                    count=teams_per_group,
                    group_id=group_id,
                ),
                standings = [],
                standings_sorted=[]
            )
            # fill up the standings.
            self.recalculate_group_standings(groups[group_id])

        database = GroupStageSchema(
            groups = groups,
            winning_teams_per_group=winning_teams_per_group,
            status=Status.IN_PROGRESS
        )

        self.commit_gs(data=database)

        # also update it in brackets.json that groupstage is active;
        brackets = self.read_meta()
        brackets.active_round = "group-stage"
        brackets.total_rounds += 1
        self.commit_meta(brackets)

        return True

    def generate_round_robin(self, teams: list, count: int, group_id: str) -> dict[str, RoundSchema]:
        """generates rounds robin for teams."""
        rounds = {}

        # if the teams count is odd.. we can add an empty team and then remove it while making rounds.
        if count % 2 != 0:
            teams = teams + [None]
            count += 1

        for round in range(1, count):
            round_matches = {}
            # for each round.
            for i in range(count // 2):
                first = teams[i]  # first in sense of next front.
                last = teams[count - 1 - i]  # last in sense of previous back.

                match = self.create_match_format(
                    team1=first,
                    team2=last,
                    match_id=str(i + 1),
                    group_id=group_id,
                    round_id=str(round),
                )

                # if one of them is None, we give them BYEs.
                if first is None or last is None:
                    match.status = Status.COMPLETED
                    match.winner_idx = 0 if last is None else 1

                round_matches[match.match_id] = match

            rounds[str(round)] = RoundSchema(matches = round_matches, status = Status.IN_PROGRESS if round == 1 else Status.PENDING)

            # shuffle it so the teams dont get matched up with the same team twice.
            teams = [teams[0]] + [teams[-1]] + teams[1:-1]
        return rounds

    def update_gs_match(
        self, match: MatchSchema
    ):
        """updates the match of the group round."""
        gs = self.read_gs()
        group = gs.groups[match.group_id]
        round_id = match.round_id
        round = group.rounds[round_id]
        round.matches[match.match_id] = match

        match.status = Status.COMPLETED

        # all the matches of same round across all the groups
        all_groups_round_completed = all(
            m.status == Status.COMPLETED
            for g in gs.groups.values()
            for m in g.rounds[round_id].matches.values()
        )

        # lets check if the round of all groups is completed.
        if all_groups_round_completed:
            # all matches are completed.
            next_round_id = str(int(round_id) + 1)

            for g in gs.groups.values():
                if round_id in g.rounds:
                    g.rounds[round_id].status = Status.COMPLETED

                if next_round_id in g.rounds:
                    # if a next round exists, turn it on.
                    g.rounds[next_round_id].status = Status.IN_PROGRESS

        # recalculate the standings
        self.recalculate_group_standings(group=group)

        # check if the whole groupstage is completed.
        if all(
            round.status == Status.COMPLETED
            for g in gs.groups.values()
            for round in g.rounds.values()
        ):
            # groupstage is completed.
            gs.status = Status.COMPLETED

            # commit now because main stage will check the status of the groupstage.
            self.commit_gs(gs)
            self.send_groupstage_brackets()
            self.send_group_stage_standings()
            # load the main stage.
            self.generate_main_stage()
            return

        self.commit_gs(gs)
        self.send_groupstage_brackets()
        self.send_group_stage_standings()

    def update_ms_match(self, match: MatchSchema):
        """updates the match of the main-stage."""
        current_round_path = self.get_active_round_path()
        current_round_data = self.read_ms(current_round_path)

        current_round_data.matches[match.match_id] = match
        match.status = Status.COMPLETED

        if all(
            match.status == Status.COMPLETED
            for match in current_round_data.matches.values()
        ):
            # all matches are completed.
            current_round_data.status = Status.COMPLETED
            # commit now because next round will check the status of the current round.
            self.commit_ms(current_round_data, current_round_path)
            self.send_mainstage_brackets()
            # load the next round only if finals has not been completed.
            if current_round_path.name == "finals.json":
                self.announce_tournament_completion()
                return
            self.generate_ms_next_round()
            return

        self.commit_ms(current_round_data, current_round_path)
        self.send_mainstage_brackets()

    def recalculate_group_standings(self, group: GroupSchema) -> None:
        """recalculates the group standings based on:
        1. points
        2. diff
        3. rounds won."""
        stats = {}
        for round in group.rounds.values():
            for match in round.matches.values():
                # add to stats
                for team in match.teams:
                    team_name = team.name
                    if team_name and team_name not in stats:
                        stats[team_name] = {
                            "id": team_name, # the team name is the id itself for uniqueness.
                            "wins": 0,
                            "loses": 0,
                            "points": 0,
                            "diff": 0,
                            "rounds_won": 0,
                            "rounds_lost": 0,
                        }

        # we put and calculate stats of the completed matches only.
        for round in group.rounds.values():
            for match in round.matches.values():
                if match.status != Status.COMPLETED:
                    continue

                if None in [team.name for team in match.teams]:
                    continue


                team1 = match.teams[0]
                team2 = match.teams[1]
                t1, t2 = team1.name, team2.name
                s1, s2 = team1.score, team2.score

                stats[t1]["rounds_won"] += s1
                stats[t1]["rounds_lost"] += s2
                stats[t2]["rounds_won"] += s2
                stats[t2]["rounds_lost"] += s1

                if match.winner_idx == team1.idx:
                    stats[t1]["wins"] += 1
                    stats[t2]["loses"] += 1
                else:
                    stats[t1]["loses"] += 1
                    stats[t2]["wins"] += 1

        # diff is tiebreaker.
        for team in stats.values():
            team["diff"] = team["rounds_won"] - team["rounds_lost"]
            team["points"] = team["wins"] * 3 - team["loses"]

        sorted_teams = sorted(
            stats.values(),
            key=lambda x: (x["points"], x["diff"], x["rounds_won"]),
            reverse=True,
        )

        group.standings = [team["id"] for team in sorted_teams]
        # this will come in help for showing the stats on leaderboard.
        group.standings_sorted = sorted_teams

    def send_group_stage_standings(self) -> None:
        """sends the group stage standings to discord webhook."""
        data = {
            "type": "group-standings",
            "season_id": self.season_id,
        }

        runner.run(data=data)

    def generate_first_round(
        self, teams: dict[str, GroupSchema] | list, winning_teams_per_group: int = 0
    ) -> RoundSchema:
        "generate first round of the main-stage."
        pairings = []
        if isinstance(teams, list):
            # there was no group stage before main-stage.
            import random

            shuffled_teams = teams.copy()
            random.shuffle(shuffled_teams)

            # make the pairings.
            for i in range(0, len(teams), 2):
                pairings.append((shuffled_teams[i], shuffled_teams[i + 1]))

        else:
            # there was a group stage.. so the teams dict is actually the dict of groups.
            # groups = teams
            groups_keys = list(teams.keys())
            groups_count = len(groups_keys)
            offset = groups_count // 2

            for i in range(groups_count):
                front_group = groups_keys[i]
                back_group = groups_keys[(i + offset) % groups_count]

                if winning_teams_per_group == 1:
                    if i < offset:
                        pairings.append(
                            (
                                teams[front_group].standings[0],
                                teams[back_group].standings[0],
                            )
                        )
                    continue

                for index in range(winning_teams_per_group // 2):
                    team1 = teams[front_group].standings[index]
                    team2 = teams[back_group].standings[
                        winning_teams_per_group - 1 - index
                    ]
                    pairings.append((team1, team2))

        # we have the pairings now.
        matches = {}
        for index, (t1, t2) in enumerate(pairings, start=1):
            matches[str(index)] = self.create_match_format(
                team1=t1, team2=t2, match_id=str(index)
            )

        return RoundSchema(matches = matches, status = Status.IN_PROGRESS)


    def generate_main_stage(self, teams: list = []):
        """generates the main stage"""
        brackets = self.read_meta()

        if teams:
            # there was no group stage before us.
            round_data = self.generate_first_round(teams)
        else:
            # there was a group stage before us.
            # to make the match to be fair, we will shuffle them first to last; like we did for group-stage matches
            # but only once per team.
            gs = self.read_gs()
            round_data = self.generate_first_round(
                gs.groups, gs.winning_teams_per_group
            )

        round_name = self.get_round_name(len(round_data.matches) * 2)
        brackets.total_rounds += 1
        brackets.active_round = round_name
        self.commit_meta(brackets)
        file_path = self.get_active_round_path()
        self.commit_ms(round_data, path=file_path)

        # send the brackets to discord.
        self.send_mainstage_brackets()

    def generate_ms_next_round(self):
        """generates the next rounds of main-stage"""
        brackets = self.read_meta()
        current_round_path = self.get_active_round_path()
        current_round_data = self.read_ms(current_round_path)
        if current_round_data.status != Status.COMPLETED:
            # the round did not complete. we cannot generate the next.
            return

        matches_data = current_round_data.matches
        next_matches = {}

        winners = [team.name for match in matches_data.values() for team in match.teams if team.idx == match.winner_idx]

        if len(winners) == 2:
            # we just finished semi-finals.
            # finals has two matches.. "1" is for 1st/2nd position between semi-finals winners
            # "2" is for 3rd position between semi-finals losers
            losers = [team.name for match in matches_data.values() for team in match.teams if team.idx == match.loser_idx]

            next_matches["1"] = self.create_match_format(
                team1=winners[0], team2=winners[1], match_id="1"
            )
            next_matches["2"] = self.create_match_format(
                team1=losers[0], team2=losers[1], match_id="2"
            )

        else:
            # standard rounds.
            match_count = 1
            for i in range(0, len(winners), 2):
                next_matches[str(match_count)] = self.create_match_format(
                    team1=winners[i], team2=winners[i + 1], match_id=str(match_count)
                )
                match_count += 1

        next_round_data = RoundSchema(matches = next_matches, status = Status.IN_PROGRESS)
        next_round_name = self.get_round_name(len(winners))
        brackets.active_round = next_round_name
        brackets.total_rounds += 1
        self.commit_meta(brackets)
        next_round_path = self.get_active_round_path()
        self.commit_ms(next_round_data, next_round_path)

    def announce_tournament_completion(self) -> None:
        """announces the tournament completion."""
        from tournament import tournament

        db = tournament.read()
        db.active_season = "0"
        tournament.commit(db)

        # TODO: announce completion with winners.

    def announce_match_start(self, team1: str, team2: str) -> None:
        """announces the match start in discord."""
        from tournament import tournament
        data = {
            "type": "match-announcement",
            "season_id": self.season_id,
            "participant_role_id": tournament.get_season(self.season_id).participant_role_id,
            "team1": team1,
            "team2": team2,
        }
        runner.run(data=data)

    def send_mainstage_brackets(self) -> None:
        """sends the mainstage brackets."""
        data = {
            "type": "main-stage",
            "season_id": self.season_id,
        }
        runner.run(data=data)

    def send_groupstage_brackets(self) -> None:
        """ sends groupstage brackets with webhooks."""
        data = {
            "type": "group-stage",
            "season_id": self.season_id,
        }
        runner.run(data=data)

    def send_results(
        self,
        match: MatchSchema,
        key: str,
    ) -> None:
        team1 = match.teams[0]
        team2 = match.teams[1]
        details = {
            "team1": team1.name,
            "team2": team2.name,
            "winner": match.teams[match.winner_idx].name,
            "score1": team1.score,
            "score2": team2.score,
            "series1": team1.series,
            "series2": team2.series,
            "key": key,
            "season_id": self.season_id,
        }
        data = {
            "type": "results",
            "details": details,
            "season_id": self.season_id,
        }
        runner.run(data=data)

    def send_players_dashboard(self) -> None:
        """sends the players dashboard."""
        data = {
            "type": "player-standings",
            "season_id": self.season_id,
        }
        runner.run(data=data)

    def get_active_round_path(self) -> Path:
        """returns the active round path."""
        brackets = self.read_meta()
        return (self.directory / "rounds" / brackets.active_round).with_suffix(
            ".json"
        )

    def read_meta(self) -> BracketsMetaSchema:
        return BracketsMetaSchema.from_dict(self.read())

    def commit_meta(self, data: BracketsMetaSchema) -> None:
        self.commit(data.to_dict())

    def read_gs(self) -> GroupStageSchema:
        return GroupStageSchema.from_dict(self.read(self.group_stage_path))

    def commit_gs(self, data: GroupStageSchema) -> None:
        self.commit(data.to_dict(), self.group_stage_path)

    def read_ms(self, path: Path) -> RoundSchema:
        return RoundSchema.from_dict(self.read(path))

    def commit_ms(self, data: RoundSchema, path: Path) -> None:
        self.commit(data.to_dict(), path)

    def list_matches(self) -> dict[str, MatchSchema]:
        """ returns the list of all the matches in active round."""
        round_path = self.get_active_round_path()
        if not round_path.exists():
            return {} # no active round.

        matches = {}

        # if the round is groupstage;
        if round_path.name == "group-stage.json":
            round_data = self.read_gs()
            for group in round_data.groups.values():
                for round in group.rounds.values():
                    if round.status == Status.IN_PROGRESS:
                        for match in round.matches.values():
                            composite_id = f"{match.group_id}-{match.round_id}-{match.match_id}"
                            matches[composite_id] = match
        else:
            round_data = self.read_ms(round_path)
            for match_id, match in round_data.matches.items():
                # match ids repeat in every round, so the key carries the round too.
                matches[f"{round_path.stem}-{match_id}"] = match

        return matches

    def give_win_to_team(self, match_index: int, team_index: int, force: bool = False) -> str:
        """gives the win to the team."""
        if team_index not in (1, 2):
            return "Team index must be either 1 or 2."
        team_index -= 1

        matches = self.list_matches()
        if not matches:
            return "No active round."

        match_ids = list(matches.keys())
        if match_index < 1 or match_index > len(match_ids):
            return f"Invalid match index. Must be between 1 and {len(match_ids)}."

        match_id = match_ids[match_index - 1]

        if match_id not in matches:
            return "No such match."

        match = matches[match_id]
        if match.status != Status.COMPLETED or force:
            from tournament import tournament
            series_length = tournament.series_length
            match.winner_idx = team_index
            match.loser_idx = 1 - team_index

            team = match.teams[team_index]
            team.score = series_length * 4
            team.series = series_length
            
            if match.group_id:
                # its a group stage match.
                self.update_gs_match(
                    match = match,
                )
            else:
                # its a main stage match.
                self.update_ms_match(
                    match = match,
                )

            # and now we can send the results to discord.
            self.send_results(match, match_id)
            self.send_players_dashboard()
            return f"Given {team.name} win."
        return "Match is already completed"

    def get_round_name(self, count: int) -> str:
        """returns the round-name by teams-count"""
        if count >= 16:
            return f"round-of-{count}"
        elif count == 8:
            return "quarter-finals"
        elif count == 4:
            return "semi-finals"
        else:
            return "finals"

    def get_team(self, team_id: str) -> TeamSchema:
        """returns the full team information dict."""
        return self.registration.read().teams[team_id]

    def create_match_format(
        self,
        team1: str,
        team2: str,
        match_id: str,
        group_id: str | None = None,
        round_id: str | None = None,
    ) -> MatchSchema:
        """match format."""
        return MatchSchema(
            match_id = match_id,
            teams = [
                MatchTeamSchema(idx=0, name=team1),
                MatchTeamSchema(idx=1, name=team2)
            ],
            group_id = group_id,
            round_id=round_id
        )