import os
import json
import asyncio
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

# ======================================
# ENV
# ======================================
load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
TEST_GUILD_ID = (os.getenv("TEST_GUILD_ID") or "").strip()
DATA_FILE = (os.getenv("DATA_FILE") or "league_data.json").strip()

FREE_AGENT_ROLE_NAME = (os.getenv("FREE_AGENT_ROLE_NAME") or "Free Agent").strip()
ROSTER_CAP = int((os.getenv("ROSTER_CAP") or "28").strip())

# Stats env
GOOGLE_SERVICE_ACCOUNT_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
SPREADSHEET_ID = (os.getenv("SPREADSHEET_ID") or "").strip()
STATSHEET_URL = (os.getenv("STATSHEET_URL") or "").strip()
SUCCESS_GIF_URL = (os.getenv("SUCCESS_GIF_URL") or "").strip()

QB_SHEET = (os.getenv("QB_SHEET") or "QB").strip()
WR_SHEET = (os.getenv("WR_SHEET") or "WR").strip()
CB_SHEET = (os.getenv("CB_SHEET") or "CB").strip()
DE_SHEET = (os.getenv("DE_SHEET") or "DE").strip()
RB_SHEET = (os.getenv("RB_SHEET") or "RB").strip()

STATS_WHITELIST_FILE = (os.getenv("STATS_WHITELIST_FILE") or "stats_whitelist.json").strip()

# Sync safety
SYNC_RETRIES = 6
SYNC_BACKOFF_MAX_SECONDS = 30

# Custom emoji parsing: <:name:id> or <a:name:id>
CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")

# ======================================
# SMALL HELPERS
# ======================================
def parse_ids(csv: str) -> Set[int]:
    return {int(x.strip()) for x in (csv or "").split(",") if x.strip().isdigit()}

OWNER_IDS: Set[int] = parse_ids(os.getenv("OWNER_IDS", ""))

def is_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator

def custom_emoji_url(emoji: str) -> Optional[str]:
    m = CUSTOM_EMOJI_RE.match((emoji or "").strip())
    if not m:
        return None
    animated = m.group(1) == "a"
    emoji_id = m.group(3)
    ext = "gif" if animated else "png"
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=256&quality=lossless"

def chunk_lines(lines: List[str], max_len: int = 900) -> List[str]:
    blocks: List[str] = []
    cur = ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_len:
            blocks.append(cur.strip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        blocks.append(cur.strip())
    return blocks

# ======================================
# EMBEDS (CORE)
# ======================================
def e_success(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=discord.Color.green())

def e_error(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=discord.Color.red())

def e_info(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=discord.Color.blurple())

def embed_offer(guild: discord.Guild, team_label: str, coach: discord.Member, player: discord.Member, team_emoji: str) -> discord.Embed:
    e = discord.Embed(
        title="📄 CONTRACT OFFER",
        description="A team has offered you a contract. Choose below.",
        color=discord.Color.blurple(),
    )
    if guild.icon:
        e.set_author(name="NAA OFFER", icon_url=guild.icon.url)
    else:
        e.set_author(name="NAA OFFER")

    thumb = custom_emoji_url(team_emoji)
    if thumb:
        e.set_thumbnail(url=thumb)

    e.add_field(name="Coach:", value=coach.mention, inline=False)
    e.add_field(name="Player:", value=player.mention, inline=False)
    e.add_field(name="Team:", value=team_label, inline=False)
    e.set_footer(text="Accepting adds the team role. Free Agent stays.")
    return e

def embed_contract_accepted(guild: discord.Guild, team_label: str, player: discord.Member, team_emoji: str) -> discord.Embed:
    e = discord.Embed(
        title="CONTRACT ACCEPTED",
        description=f"{player.mention} accepted the contract.",
        color=discord.Color.green(),
    )
    if guild.icon:
        e.set_author(name="CONTRACT ACCEPTED ✅", icon_url=guild.icon.url)
    else:
        e.set_author(name="CONTRACT ACCEPTED ✅")

    thumb = custom_emoji_url(team_emoji)
    if thumb:
        e.set_thumbnail(url=thumb)

    e.add_field(name="Status", value="✅ CONTRACT ACCEPTED", inline=True)
    e.add_field(name="Team", value=f"**{team_label}**", inline=True)
    e.add_field(name="Player", value=player.mention, inline=False)
    e.set_footer(text="Team role added. Free Agent kept.")
    return e

def embed_contract_denied(guild: discord.Guild, player: discord.Member, team_emoji: str, team_label: str) -> discord.Embed:
    e = discord.Embed(
        title="CONTRACT DENIED",
        description=f"{player.mention} denied the contract.",
        color=discord.Color.red(),
    )
    if guild.icon:
        e.set_author(name="CONTRACT DENIED ❌", icon_url=guild.icon.url)
    else:
        e.set_author(name="CONTRACT DENIED ❌")

    thumb = custom_emoji_url(team_emoji)
    if thumb:
        e.set_thumbnail(url=thumb)

    e.add_field(name="Status", value="❌ CONTRACT DENIED", inline=True)
    e.add_field(name="Team", value=f"**{team_label}**", inline=True)
    e.add_field(name="Player", value=player.mention, inline=False)
    return e

def roster_embed_like_photo(
    guild: discord.Guild,
    team_name: str,
    team_emoji: str,
    count: int,
    cap: int,
    players_block: str,
    page: int = 1,
    total_pages: int = 1,
) -> discord.Embed:
    title = "Player Roster" if total_pages == 1 else f"Player Roster (Page {page}/{total_pages})"
    e = discord.Embed(title=title, color=discord.Color.red())

    if guild.icon:
        e.set_author(name="", icon_url=guild.icon.url)

    thumb = custom_emoji_url(team_emoji)
    if thumb:
        e.set_thumbnail(url=thumb)

    e.description = (
        f"**{team_name}**\n\n"
        f"**{team_name} Roster**\n"
        f"**Player Count : {count}/{cap}**\n\n"
        f"**Players**\n"
        f"{players_block or '_None_'}"
    )
    return e

def embed_release_alert(guild: discord.Guild, player: discord.Member, team_label: str, team_emoji: str, released_by: discord.Member) -> discord.Embed:
    # Matches your “photo 3” style: RELEASE ALERT, fields Player/Team/Released By, team thumb top-right
    e = discord.Embed(
        title="RELEASE ALERT",
        description=f"{player.mention} was released from  {team_label}.",
        color=discord.Color.orange(),
    )
    if guild.icon:
        e.set_author(name="RELEASE ALERT", icon_url=guild.icon.url)
    else:
        e.set_author(name="RELEASE ALERT")

    thumb = custom_emoji_url(team_emoji)
    if thumb:
        e.set_thumbnail(url=thumb)

    e.add_field(name="Player", value=player.mention, inline=True)
    e.add_field(name="Team", value=team_label, inline=True)
    e.add_field(name="Released By", value=released_by.mention, inline=False)
    return e

# ======================================
# STATS EMBEDS
# ======================================
def make_success_view() -> discord.ui.View:
    view = discord.ui.View()
    if STATSHEET_URL:
        view.add_item(discord.ui.Button(label="View Statsheet", url=STATSHEET_URL))
    return view

def make_success_embed(player_name: str) -> discord.Embed:
    embed = discord.Embed(
        description=f"Successfully uploaded stats for **{player_name}**.",
        color=discord.Color.green(),
    )
    if SUCCESS_GIF_URL:
        embed.set_author(name="Success", icon_url=SUCCESS_GIF_URL)
    else:
        embed.set_author(name="Success")
    return embed

def make_error_embed(msg: str) -> discord.Embed:
    embed = discord.Embed(description=msg, color=discord.Color.red())
    embed.set_author(name="Error")
    return embed

# ======================================
# STATS PERMISSIONS
# ======================================
def load_stats_whitelist() -> Set[int]:
    if not os.path.exists(STATS_WHITELIST_FILE):
        return set()
    try:
        with open(STATS_WHITELIST_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("user_ids", []))
    except Exception:
        return set()

def save_stats_whitelist(ids: Set[int]) -> None:
    with open(STATS_WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"user_ids": sorted(list(ids))}, f, indent=2)

STATS_WHITELIST: Set[int] = load_stats_whitelist()

def stats_is_owner(uid: int) -> bool:
    return uid in OWNER_IDS

def stats_is_allowed(uid: int) -> bool:
    return uid in OWNER_IDS or uid in STATS_WHITELIST

# ======================================
# GOOGLE SHEETS (UPDATED + RB AUTO DETECT)
# ======================================
def get_client() -> gspread.Client:
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON missing in .env")
    if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        raise RuntimeError(f"No such file: '{GOOGLE_SERVICE_ACCOUNT_JSON}' (put it next to bot.py)")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

def normalize(text: str) -> str:
    return re.sub(r"[\s\._\-]+", "", (text or "").strip().lower())

def find_column(headers: List[str], name: str) -> Optional[int]:
    want = normalize(name)
    for i, h in enumerate(headers):
        if normalize(h) == want:
            return i + 1
    return None

def to_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except Exception:
        return 0

def stack_stats(ws: gspread.Worksheet, name_column: str, stat_data: dict) -> None:
    headers = ws.row_values(1)
    if not headers:
        raise RuntimeError("Header row missing in sheet.")

    name_col = find_column(headers, name_column)
    if not name_col:
        raise RuntimeError(f"Column '{name_column}' not found.")

    all_rows = ws.get_all_values()
    target_row = None

    for i in range(2, len(all_rows) + 1):
        row = all_rows[i - 1]
        if name_col - 1 < len(row):
            if normalize(row[name_col - 1]) == normalize(stat_data["name"]):
                target_row = i
                break

    if target_row is None:
        target_row = len(all_rows) + 1

    for key, value in stat_data.items():
        if key == "name":
            continue

        col = find_column(headers, key)
        if not col:
            continue

        current = 0
        if target_row <= len(all_rows):
            row = all_rows[target_row - 1]
            if col - 1 < len(row):
                current = to_int(row[col - 1])

        ws.update(
            range_name=gspread.utils.rowcol_to_a1(target_row, col),
            values=[[current + int(value)]],
            value_input_option="USER_ENTERED",
        )

    ws.update(
        range_name=gspread.utils.rowcol_to_a1(target_row, name_col),
        values=[[stat_data["name"]]],
        value_input_option="USER_ENTERED",
    )

RB_HEADERS = {
    "name": ["Running Back", "RB", "RunningBack"],
    "carries": ["Carries", "Car", "Attempts"],
    "rush_yards": ["Rush Yards", "RushYards", "Rushing Yards"],
    "rush_td": ["Rush TD", "RushTD", "Rushing TD", "Rush TDs"],
    "receptions": ["Receptions", "Rec", "Catches"],
    "rec_yards": ["Rec Yards", "RecYards", "Receiving Yards"],
    "rec_td": ["Rec TD", "RecTD", "Receiving TD", "Rec TDs"],
    "fumbles": ["Fumbles", "Fum", "Fumbles Lost", "FumblesLost"],
}

def detect_rb_columns(ws: gspread.Worksheet, scan_rows: int = 10) -> Tuple[int, dict]:
    best_row = 1
    best_cols = {}
    best_score = -1

    for r in range(1, scan_rows + 1):
        row = ws.row_values(r)
        cols = {}

        for c, cell in enumerate(row, start=1):
            cell_n = normalize(cell)
            if not cell_n:
                continue

            for key, options in RB_HEADERS.items():
                for opt in options:
                    if cell_n == normalize(opt):
                        cols[key] = c

        score = len(cols)
        if score > best_score:
            best_score = score
            best_row = r
            best_cols = cols

    if best_score < 3:
        best_row = 1
        best_cols = {
            "name": 1,
            "carries": 2,
            "rush_yards": 3,
            "rush_td": 4,
            "receptions": 5,
            "rec_yards": 6,
            "rec_td": 7,
            "fumbles": 8,
        }

    return best_row, best_cols

def stack_rb(
    ws: gspread.Worksheet,
    runningback: str,
    carries: int,
    rush_yards: int,
    rush_td: int,
    receptions: int,
    rec_yards: int,
    rec_td: int,
    fumbles: int,
) -> None:
    header_row, cols = detect_rb_columns(ws, scan_rows=12)

    col_name = cols.get("name", 1)
    col_carries = cols.get("carries", col_name + 1)
    col_rush_yards = cols.get("rush_yards", col_carries + 1)
    col_rush_td = cols.get("rush_td", col_rush_yards + 1)
    col_receptions = cols.get("receptions", col_rush_td + 1)
    col_rec_yards = cols.get("rec_yards", col_receptions + 1)
    col_rec_td = cols.get("rec_td", col_rec_yards + 1)
    col_fumbles = cols.get("fumbles", col_rec_td + 1)

    all_rows = ws.get_all_values()

    target_row = None
    for r in range(header_row + 1, len(all_rows) + 1):
        row = all_rows[r - 1]
        cell = row[col_name - 1] if (col_name - 1) < len(row) else ""
        if normalize(cell) == normalize(runningback):
            target_row = r
            break

    if target_row is None:
        target_row = len(all_rows) + 1
        current_row = []
    else:
        current_row = all_rows[target_row - 1]

    def cur(col: int) -> int:
        i = col - 1
        return to_int(current_row[i]) if i < len(current_row) else 0

    ws.update(gspread.utils.rowcol_to_a1(target_row, col_name), [[runningback]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_carries), [[cur(col_carries) + carries]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_rush_yards), [[cur(col_rush_yards) + rush_yards]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_rush_td), [[cur(col_rush_td) + rush_td]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_receptions), [[cur(col_receptions) + receptions]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_rec_yards), [[cur(col_rec_yards) + rec_yards]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_rec_td), [[cur(col_rec_td) + rec_td]], value_input_option="USER_ENTERED")
    ws.update(gspread.utils.rowcol_to_a1(target_row, col_fumbles), [[cur(col_fumbles) + fumbles]], value_input_option="USER_ENTERED")

# ======================================
# STORE (Teams + Whitelists + Channels)
# ======================================
@dataclass
class Team:
    team_name: str
    team_role_id: int
    emoji: str = ""

class LeagueStore:
    """
    league_data.json structure:
    {
      "offer_role_ids": [..],
      "release_role_ids": [..],
      "offer_channel_id": 123 or null,
      "release_channel_id": 456 or null,
      "teams": {
        "normalized_team_name": {
          "team_name": "...",
          "team_role_id": 999,
          "emoji": "<:team:123...>"
        }
      }
    }
    """
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

        self.offer_role_ids: List[int] = []
        self.release_role_ids: List[int] = []
        self.offer_channel_id: Optional[int] = None
        self.release_channel_id: Optional[int] = None
        self.teams_by_key: Dict[str, Team] = {}

    @staticmethod
    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    async def load(self) -> None:
        async with self._lock:
            self.offer_role_ids = []
            self.release_role_ids = []
            self.offer_channel_id = None
            self.release_channel_id = None
            self.teams_by_key = {}

            if not os.path.exists(self.path):
                return

            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                return

            self.offer_role_ids = [int(x) for x in raw.get("offer_role_ids", []) if str(x).isdigit()]
            self.release_role_ids = [int(x) for x in raw.get("release_role_ids", []) if str(x).isdigit()]

            oc = raw.get("offer_channel_id", None)
            rc = raw.get("release_channel_id", None)
            self.offer_channel_id = int(oc) if str(oc).isdigit() else None
            self.release_channel_id = int(rc) if str(rc).isdigit() else None

            teams_raw = raw.get("teams", {}) or {}
            for _, t in teams_raw.items():
                try:
                    team = Team(
                        team_name=str(t["team_name"]),
                        team_role_id=int(t["team_role_id"]),
                        emoji=str(t.get("emoji", "")) if t.get("emoji") is not None else "",
                    )
                    self.teams_by_key[self._norm(team.team_name)] = team
                except Exception:
                    continue

    async def save(self) -> None:
        async with self._lock:
            payload = {
                "offer_role_ids": self.offer_role_ids,
                "release_role_ids": self.release_role_ids,
                "offer_channel_id": self.offer_channel_id,
                "release_channel_id": self.release_channel_id,
                "teams": {k: asdict(v) for k, v in self.teams_by_key.items()},
            }
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

    def get_team_by_role_id(self, role_id: int) -> Optional[Team]:
        for t in self.teams_by_key.values():
            if t.team_role_id == role_id:
                return t
        return None

    def get_team_by_name(self, team_name: str) -> Optional[Team]:
        return self.teams_by_key.get(self._norm(team_name))

    def format_team_label(self, team: Team) -> str:
        return f"{team.emoji} {team.team_name}".strip() if team.emoji else team.team_name

    async def add_team_from_role(self, team_role: discord.Role, emoji: str) -> Team:
        team_name = team_role.name
        key = self._norm(team_name)

        if key in self.teams_by_key:
            raise ValueError("That team is already registered.")
        if self.get_team_by_role_id(team_role.id):
            raise ValueError("That team role is already linked to another team.")

        team = Team(team_name=team_name, team_role_id=team_role.id, emoji=(emoji or "").strip())
        self.teams_by_key[key] = team
        await self.save()
        return team

    async def remove_team(self, team_role_id: int) -> None:
        target_key = None
        for k, t in self.teams_by_key.items():
            if t.team_role_id == team_role_id:
                target_key = k
                break
        if not target_key:
            raise ValueError("That team is not registered.")
        del self.teams_by_key[target_key]
        await self.save()

    # Whitelists
    def member_has_any_role(self, member: discord.Member, allowed_role_ids: List[int]) -> bool:
        if not allowed_role_ids:
            return False
        member_roles = {r.id for r in member.roles}
        return any(rid in member_roles for rid in allowed_role_ids)

    def offer_allowed(self, member: discord.Member) -> bool:
        return self.member_has_any_role(member, self.offer_role_ids)

    def release_allowed(self, member: discord.Member) -> bool:
        return self.member_has_any_role(member, self.release_role_ids)

    async def offer_whitelist_add(self, role_id: int) -> None:
        if role_id not in self.offer_role_ids:
            self.offer_role_ids.append(role_id)
            await self.save()

    async def offer_whitelist_remove(self, role_id: int) -> None:
        if role_id in self.offer_role_ids:
            self.offer_role_ids.remove(role_id)
            await self.save()

    async def release_whitelist_add(self, role_id: int) -> None:
        if role_id not in self.release_role_ids:
            self.release_role_ids.append(role_id)
            await self.save()

    async def release_whitelist_remove(self, role_id: int) -> None:
        if role_id in self.release_role_ids:
            self.release_role_ids.remove(role_id)
            await self.save()

    # Channels
    async def set_offer_channel(self, channel_id: Optional[int]) -> None:
        self.offer_channel_id = channel_id
        await self.save()

    async def set_release_channel(self, channel_id: Optional[int]) -> None:
        self.release_channel_id = channel_id
        await self.save()

    # Team auto-detect for a member (must be on exactly 1 registered team)
    def detect_member_team(self, member: discord.Member) -> Tuple[Optional[Team], List[Team]]:
        member_role_ids = {r.id for r in member.roles}
        matches: List[Team] = []
        for t in self.teams_by_key.values():
            if t.team_role_id in member_role_ids:
                matches.append(t)
        if len(matches) == 1:
            return matches[0], matches
        return None, matches

# ======================================
# UI: Offer buttons
# ======================================
class OfferView(discord.ui.View):
    def __init__(
        self,
        offered_user_id: int,
        team_role_id: int,
        free_agent_role_id: int,
        team_label: str,
        team_emoji: str,
        roster_cap: int,
        timeout: int = 180,
    ):
        super().__init__(timeout=timeout)
        self.offered_user_id = offered_user_id
        self.team_role_id = team_role_id
        self.free_agent_role_id = free_agent_role_id
        self.team_label = team_label
        self.team_emoji = team_emoji
        self.roster_cap = roster_cap

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.offered_user_id:
            await interaction.response.send_message(
                embed=e_error("⛔ Not For You", "Only the offered player can click these buttons."),
                ephemeral=True,
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="Accept ✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild = interaction.guild
        if not guild:
            return

        member = guild.get_member(self.offered_user_id)
        team_role = guild.get_role(self.team_role_id)
        fa_role = guild.get_role(self.free_agent_role_id)

        if not member or not team_role or not fa_role:
            self._disable_buttons()
            await interaction.edit_original_response(
                embed=e_error("❌ Offer Error", "Roles/member missing (deleted or not found)."),
                view=self,
            )
            return

        # Must still be Free Agent
        if fa_role not in member.roles:
            self._disable_buttons()
            await interaction.edit_original_response(
                embed=e_error("❌ Offer Invalid", "You are no longer a Free Agent."),
                view=self,
            )
            return

        # Roster cap check
        try:
            if not guild.chunked:
                await guild.chunk(cache=True)
        except Exception:
            pass

        team_members = [m for m in guild.members if team_role in m.roles]
        if len(team_members) >= self.roster_cap:
            self._disable_buttons()
            await interaction.edit_original_response(
                embed=e_error("❌ Team Full", f"**{self.team_label}** is full ({len(team_members)}/{self.roster_cap})."),
                view=self,
            )
            return

        # Keep Free Agent role, add team role only
        try:
            if team_role not in member.roles:
                await member.add_roles(team_role, reason=f"Accepted contract to {self.team_label}")
        except discord.Forbidden:
            self._disable_buttons()
            await interaction.edit_original_response(
                embed=e_error(
                    "❌ Bot Can’t Manage Roles",
                    "Fix:\n• Give bot **Manage Roles**\n• Put bot role ABOVE team roles",
                ),
                view=self,
            )
            return
        except discord.HTTPException as ex:
            self._disable_buttons()
            await interaction.edit_original_response(
                embed=e_error("❌ Discord Error", f"Failed to add role: {ex}"),
                view=self,
            )
            return

        # ✅ ONLY edit original message (no duplicates)
        self._disable_buttons()
        await interaction.edit_original_response(
            embed=embed_contract_accepted(guild, self.team_label, member, self.team_emoji),
            view=self,
        )

    @discord.ui.button(label="Decline ❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild = interaction.guild
        if not guild:
            return
        self._disable_buttons()
        await interaction.edit_original_response(
            embed=embed_contract_denied(guild, interaction.user, self.team_emoji, self.team_label),
            view=self,
        )

# ======================================
# GROUPS: WHITELISTS + CHANNEL SETTERS
# ======================================
class OfferWhitelistGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="offerwhitelist", description="Manage /offer whitelist roles (ADMIN ONLY).")

    @app_commands.command(name="add", description="Add a role that can use /offer (GM/HC/AHC).")
    async def add(self, interaction: discord.Interaction, role: discord.Role):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.offer_whitelist_add(role.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Offer Whitelist Updated", f"Added {role.mention}."), ephemeral=True)

    @app_commands.command(name="remove", description="Remove a role from /offer whitelist.")
    async def remove(self, interaction: discord.Interaction, role: discord.Role):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.offer_whitelist_remove(role.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Offer Whitelist Updated", f"Removed {role.mention}."), ephemeral=True)

    @app_commands.command(name="list", description="List roles that can use /offer.")
    async def list_roles(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)

        store = interaction.client.store  # type: ignore
        if not store.offer_role_ids:
            return await interaction.response.send_message(embed=e_info("📋 Offer Whitelist", "None yet."), ephemeral=True)

        lines = []
        for rid in store.offer_role_ids:
            r = interaction.guild.get_role(rid) if interaction.guild else None
            lines.append(f"• {r.mention if r else f'`{rid}`'}")

        await interaction.response.send_message(embed=e_info("📋 Offer Whitelist", "\n".join(lines)), ephemeral=True)

class ReleaseWhitelistGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="releasewhitelist", description="Manage /release whitelist roles (ADMIN ONLY).")

    @app_commands.command(name="add", description="Add a role that can use /release (GM/HC/AHC).")
    async def add(self, interaction: discord.Interaction, role: discord.Role):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.release_whitelist_add(role.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Release Whitelist Updated", f"Added {role.mention}."), ephemeral=True)

    @app_commands.command(name="remove", description="Remove a role from /release whitelist.")
    async def remove(self, interaction: discord.Interaction, role: discord.Role):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.release_whitelist_remove(role.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Release Whitelist Updated", f"Removed {role.mention}."), ephemeral=True)

    @app_commands.command(name="list", description="List roles that can use /release.")
    async def list_roles(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)

        store = interaction.client.store  # type: ignore
        if not store.release_role_ids:
            return await interaction.response.send_message(embed=e_info("📋 Release Whitelist", "None yet."), ephemeral=True)

        lines = []
        for rid in store.release_role_ids:
            r = interaction.guild.get_role(rid) if interaction.guild else None
            lines.append(f"• {r.mention if r else f'`{rid}`'}")

        await interaction.response.send_message(embed=e_info("📋 Release Whitelist", "\n".join(lines)), ephemeral=True)

class OfferChannelGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="offerchannel", description="Set where contract offers are posted (ADMIN ONLY).")

    @app_commands.command(name="set", description="Set the channel where /offer posts the contract message.")
    async def set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.set_offer_channel(channel.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Offer Channel Set", f"Offers will post in {channel.mention}."), ephemeral=True)

    @app_commands.command(name="clear", description="Clear the offer channel (offers post where command is used).")
    async def clear(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.set_offer_channel(None)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Offer Channel Cleared", "Offers will post in the channel you run /offer."), ephemeral=True)

class ReleaseChannelGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="releasechannel", description="Set where release alerts are posted (ADMIN ONLY).")

    @app_commands.command(name="set", description="Set the channel where /release posts release alerts.")
    async def set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.set_release_channel(channel.id)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Release Channel Set", f"Release alerts will post in {channel.mention}."), ephemeral=True)

    @app_commands.command(name="clear", description="Clear the release channel (release alerts post where command is used).")
    async def clear(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)
        await interaction.client.store.set_release_channel(None)  # type: ignore
        await interaction.response.send_message(embed=e_success("✅ Release Channel Cleared", "Release alerts will post in the channel you run /release."), ephemeral=True)

# ======================================
# GROUP: STATS WHITELIST (OWNERS ONLY)
# ======================================
class StatsWhitelistGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="statswhitelist", description="Manage /insert whitelist (OWNERS ONLY).")

    @app_commands.command(name="add", description="Allow a user to use /insert stats commands.")
    async def add(self, interaction: discord.Interaction, user: discord.User):
        if not stats_is_owner(interaction.user.id):
            return await interaction.response.send_message(embed=make_error_embed("Owners only."), ephemeral=True)

        STATS_WHITELIST.add(user.id)
        save_stats_whitelist(STATS_WHITELIST)
        await interaction.response.send_message(embed=make_success_embed(str(user)), view=make_success_view(), ephemeral=True)

    @app_commands.command(name="remove", description="Remove a user from /insert whitelist.")
    async def remove(self, interaction: discord.Interaction, user: discord.User):
        if not stats_is_owner(interaction.user.id):
            return await interaction.response.send_message(embed=make_error_embed("Owners only."), ephemeral=True)

        if user.id in OWNER_IDS:
            return await interaction.response.send_message(embed=make_error_embed("Cannot remove an owner."), ephemeral=True)

        STATS_WHITELIST.discard(user.id)
        save_stats_whitelist(STATS_WHITELIST)
        await interaction.response.send_message(embed=make_success_embed(str(user)), view=make_success_view(), ephemeral=True)

    @app_commands.command(name="list", description="List all users allowed to use /insert.")
    async def list_users(self, interaction: discord.Interaction):
        if not stats_is_owner(interaction.user.id):
            return await interaction.response.send_message(embed=make_error_embed("Owners only."), ephemeral=True)

        if not STATS_WHITELIST:
            return await interaction.response.send_message(embed=e_info("Stats Whitelist", "None"), ephemeral=True)

        mention_lines = [f"- <@{uid}>" for uid in sorted(STATS_WHITELIST)]
        embed = discord.Embed(title="Stats Whitelist", description="\n".join(mention_lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ======================================
# GROUP: /insert (STATS COMMANDS) — PUBLIC RESULTS
# ======================================
class InsertGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="insert", description="Insert player stats.")

    async def process(self, interaction: discord.Interaction, sheet: str, name_column: str, name: str, stats_dict: dict):
        if not stats_is_allowed(interaction.user.id):
            return await interaction.response.send_message(embed=make_error_embed("Not allowed."), ephemeral=True)

        await interaction.response.defer()  # PUBLIC

        try:
            def run():
                ws = get_client().open_by_key(SPREADSHEET_ID).worksheet(sheet)
                stack_stats(ws, name_column, stats_dict)

            await asyncio.to_thread(run)
            await interaction.followup.send(embed=make_success_embed(name), view=make_success_view())  # PUBLIC
        except Exception as e:
            await interaction.followup.send(embed=make_error_embed(str(e)))  # PUBLIC

    @app_commands.command(name="qb-stats", description="Insert QB stats into the QB sheet (adds to totals).")
    async def qb(self, interaction: discord.Interaction, quarterback: str, comp: int, att: int, yards: int, tds: int, ints: int, sacks: int):
        await self.process(interaction, QB_SHEET, "Quarterback", quarterback,
                           {"name": quarterback, "Comp": comp, "Att": att, "Yards": yards, "TDs": tds, "INTs": ints, "Sacks": sacks})

    @app_commands.command(name="wr-stats", description="Insert WR stats into the WR sheet (adds to totals).")
    async def wr(self, interaction: discord.Interaction, widereceiver: str, catches: int, targets: int, tds: int, yac: int, yards: int):
        await self.process(interaction, WR_SHEET, "WideReceiver", widereceiver,
                           {"name": widereceiver, "Catches": catches, "Targets": targets, "TDs": tds, "YAC": yac, "Yards": yards})

    @app_commands.command(name="cb-stats", description="Insert CB stats into the CB sheet (adds to totals).")
    async def cb(self, interaction: discord.Interaction, cornerback: str, ints: int, targets: int, swats: int, tds: int, comp_allowed: int):
        await self.process(interaction, CB_SHEET, "Cornerback", cornerback,
                           {"name": cornerback, "INTs": ints, "Targets": targets, "Swats": swats, "TDs": tds, "CompAllowed": comp_allowed})

    @app_commands.command(name="de-stats", description="Insert DE stats into the DE sheet (adds to totals).")
    async def de(self, interaction: discord.Interaction, defensiveend: str, misses: int, sacks: int, safeties: int):
        await self.process(interaction, DE_SHEET, "DefensiveEnd", defensiveend,
                           {"name": defensiveend, "Misses": misses, "Sacks": sacks, "Safeties": safeties})

    @app_commands.command(name="rb-stats", description="Insert RB stats into the RB sheet (AUTO-detect headers).")
    async def rb(self, interaction: discord.Interaction, runningback: str, carries: int, rush_yards: int, rush_td: int,
                 receptions: int, rec_yards: int, rec_td: int, fumbles: int):

        if not stats_is_allowed(interaction.user.id):
            return await interaction.response.send_message(embed=make_error_embed("Not allowed."), ephemeral=True)

        await interaction.response.defer()  # PUBLIC

        try:
            def run():
                ws = get_client().open_by_key(SPREADSHEET_ID).worksheet(RB_SHEET)
                stack_rb(ws, runningback, carries, rush_yards, rush_td, receptions, rec_yards, rec_td, fumbles)

            await asyncio.to_thread(run)
            await interaction.followup.send(embed=make_success_embed(runningback), view=make_success_view())  # PUBLIC
        except Exception as e:
            await interaction.followup.send(embed=make_error_embed(str(e)))  # PUBLIC

# ======================================
# BOT
# ======================================
class NaaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = LeagueStore(DATA_FILE)

    async def setup_hook(self):
        await self.store.load()

        # Groups
        self.tree.add_command(OfferWhitelistGroup())
        self.tree.add_command(ReleaseWhitelistGroup())
        self.tree.add_command(OfferChannelGroup())
        self.tree.add_command(ReleaseChannelGroup())
        self.tree.add_command(InsertGroup())
        self.tree.add_command(StatsWhitelistGroup())

        # Fast dev sync
        if not TEST_GUILD_ID.isdigit():
            print("❌ TEST_GUILD_ID missing/invalid. Put it in .env")
            return

        guild = discord.Object(id=int(TEST_GUILD_ID))
        await self._sync_to_guild_with_retries(guild)

    async def _sync_to_guild_with_retries(self, guild: discord.Object):
        for attempt in range(1, SYNC_RETRIES + 1):
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"✅ Synced {len(synced)} guild commands to {guild.id}")
                return
            except discord.HTTPException as e:
                wait = min(2 ** attempt, SYNC_BACKOFF_MAX_SECONDS)
                print(f"⚠️ Sync failed attempt {attempt}/{SYNC_RETRIES}: {e} — retry in {wait}s")
                await asyncio.sleep(wait)
        print("❌ Sync failed after retries. Commands may still appear later.")

bot = NaaBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ======================================
# COMMANDS (CORE)
# ======================================
@bot.tree.command(name="ping", description="Health check.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(embed=e_info("🏓 Pong", "Bot is alive."), ephemeral=True)

# ----- Admin-only team registry -----
@bot.tree.command(name="addteam", description="(ADMIN ONLY) Register a team role + emoji in the database.")
@app_commands.describe(role="Select the team role", emoji="Team emoji (custom emoji recommended for thumbnail)")
async def addteam(interaction: discord.Interaction, role: discord.Role, emoji: str):
    if not interaction.guild:
        return await interaction.response.send_message(embed=e_error("❌ Error", "Use this in a server."), ephemeral=True)
    if not is_admin(interaction):
        return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)

    try:
        team = await bot.store.add_team_from_role(role, emoji)
        label = bot.store.format_team_label(team)
        thumb = custom_emoji_url(team.emoji)
        thumb_note = "✅ Thumbnail will show (custom emoji)" if thumb else "⚠️ Thumbnail won't show (unicode emoji has no image URL)"

        await interaction.response.send_message(
            embed=e_success("✅ Team Added", f"**Team:** {label}\n**Role:** {role.mention}\n{thumb_note}"),
            ephemeral=True,
        )
    except ValueError as ex:
        await interaction.response.send_message(embed=e_error("❌ Addteam Failed", str(ex)), ephemeral=True)

@bot.tree.command(name="removeteam", description="(ADMIN ONLY) Remove a team from the database.")
@app_commands.describe(team="Select the team role to remove")
async def removeteam(interaction: discord.Interaction, team: discord.Role):
    if not interaction.guild:
        return await interaction.response.send_message(embed=e_error("❌ Error", "Use this in a server."), ephemeral=True)
    if not is_admin(interaction):
        return await interaction.response.send_message(embed=e_error("⛔ No Permission", "Admins only."), ephemeral=True)

    try:
        await bot.store.remove_team(team.id)
        await interaction.response.send_message(embed=e_success("✅ Team Removed", f"Removed {team.mention} from the database."), ephemeral=True)
    except ValueError as ex:
        await interaction.response.send_message(embed=e_error("❌ Remove Failed", str(ex)), ephemeral=True)

# ----- Offer (AUTO DETECT TEAM) -----
@bot.tree.command(name="offer", description="Offer a Free Agent a contract (AUTO-detects your team).")
@app_commands.describe(player="Select the Free Agent (Discord member) to offer")
async def offer(interaction: discord.Interaction, player: discord.Member):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message(embed=e_error("❌ Error", "Use this in a server."), ephemeral=True)

    # Whitelist check for offer
    if not bot.store.offer_role_ids:
        return await interaction.response.send_message(
            embed=e_error("⛔ Not Set Up", "Admins must run `/offerwhitelist add` for GM/HC/AHC roles first."),
            ephemeral=True,
        )
    if not bot.store.offer_allowed(interaction.user):
        return await interaction.response.send_message(embed=e_error("⛔ No Permission", "You can’t use `/offer`."), ephemeral=True)

    # Auto-detect team from coach roles
    coach_team, matches = bot.store.detect_member_team(interaction.user)
    if coach_team is None:
        if len(matches) == 0:
            return await interaction.response.send_message(
                embed=e_error("❌ No Team Detected", "You don’t have a registered team role. (Admin must /addteam your team role)"),
                ephemeral=True,
            )
        return await interaction.response.send_message(
            embed=e_error("❌ Multiple Teams Detected", "You have more than 1 registered team role. You must only be on 1 team."),
            ephemeral=True,
        )

    guild = interaction.guild

    # Resolve roles
    team_role = guild.get_role(coach_team.team_role_id)
    if not team_role:
        return await interaction.response.send_message(embed=e_error("❌ Team Role Missing", "Your team role was deleted."), ephemeral=True)

    free_agent_role = discord.utils.get(guild.roles, name=FREE_AGENT_ROLE_NAME)
    if not free_agent_role:
        return await interaction.response.send_message(embed=e_error("❌ Missing Role", f"`{FREE_AGENT_ROLE_NAME}` role not found."), ephemeral=True)

    # Player must be Free Agent
    if free_agent_role not in player.roles:
        return await interaction.response.send_message(embed=e_error("❌ Not a Free Agent", f"{player.mention} is not a Free Agent."), ephemeral=True)

    # Pre-check roster cap
    try:
        if not guild.chunked:
            await guild.chunk(cache=True)
    except Exception:
        pass

    current_team_members = [m for m in guild.members if team_role in m.roles]
    if len(current_team_members) >= ROSTER_CAP:
        return await interaction.response.send_message(
            embed=e_error("❌ Team Full", f"**{bot.store.format_team_label(coach_team)}** is full ({len(current_team_members)}/{ROSTER_CAP})."),
            ephemeral=True,
        )

    team_label = bot.store.format_team_label(coach_team)
    team_emoji = coach_team.emoji

    view = OfferView(
        offered_user_id=player.id,
        team_role_id=team_role.id,
        free_agent_role_id=free_agent_role.id,
        team_label=team_label,
        team_emoji=team_emoji,
        roster_cap=ROSTER_CAP,
        timeout=180,
    )

    # Offer post channel (separate)
    offer_channel: Optional[discord.abc.Messageable] = None
    if bot.store.offer_channel_id and interaction.guild:
        ch = interaction.guild.get_channel(bot.store.offer_channel_id)
        if isinstance(ch, discord.TextChannel):
            offer_channel = ch

    if offer_channel is None:
        offer_channel = interaction.channel

    # Send the offer message THERE, and only confirm ephemerally to coach
    msg = await offer_channel.send(
        content=f"{player.mention}, choose below:",
        embed=embed_offer(guild, team_label, interaction.user, player, team_emoji),
        view=view,
    )

    await interaction.response.send_message(
        embed=e_success("✅ Offer Sent", f"Sent contract offer for {player.mention} as **{team_label}**."),
        ephemeral=True,
    )

# ----- Release (WHITELIST, AUTO TEAM, ALERT CHANNEL ONLY) -----
@bot.tree.command(name="release", description="Release a player from your team (whitelist required).")
@app_commands.describe(player="Select the player to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message(embed=e_error("❌ Error", "Use this in a server."), ephemeral=True)

    # Whitelist check for release
    if not bot.store.release_role_ids:
        return await interaction.response.send_message(
            embed=e_error("⛔ Not Set Up", "Admins must run `/releasewhitelist add` for GM/HC/AHC roles first."),
            ephemeral=True,
        )
    if not bot.store.release_allowed(interaction.user):
        return await interaction.response.send_message(embed=e_error("⛔ No Permission", "You can’t use `/release`."), ephemeral=True)

    # Auto-detect releaser team
    releaser_team, matches = bot.store.detect_member_team(interaction.user)
    if releaser_team is None:
        if len(matches) == 0:
            return await interaction.response.send_message(
                embed=e_error("❌ No Team Detected", "You don’t have a registered team role. (Admin must /addteam your team role)"),
                ephemeral=True,
            )
        return await interaction.response.send_message(
            embed=e_error("❌ Multiple Teams Detected", "You have more than 1 registered team role. You must only be on 1 team."),
            ephemeral=True,
        )

    guild = interaction.guild
    team_role = guild.get_role(releaser_team.team_role_id)
    if not team_role:
        return await interaction.response.send_message(embed=e_error("❌ Team Role Missing", "Your team role was deleted."), ephemeral=True)

    # Target must be on that team
    if team_role not in player.roles:
        return await interaction.response.send_message(
            embed=e_error("❌ Not On Team", f"{player.mention} is not on **{bot.store.format_team_label(releaser_team)}**."),
            ephemeral=True,
        )

    free_agent_role = discord.utils.get(guild.roles, name=FREE_AGENT_ROLE_NAME)
    if not free_agent_role:
        return await interaction.response.send_message(embed=e_error("❌ Missing Role", f"`{FREE_AGENT_ROLE_NAME}` role not found."), ephemeral=True)

    # Remove team role, add FA role
    try:
        await player.remove_roles(team_role, reason=f"Released by {interaction.user}")
        if free_agent_role not in player.roles:
            await player.add_roles(free_agent_role, reason=f"Released by {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message(
            embed=e_error("❌ Bot Can’t Manage Roles", "Give bot **Manage Roles** and put bot role ABOVE team roles."),
            ephemeral=True,
        )
    except discord.HTTPException as ex:
        return await interaction.response.send_message(embed=e_error("❌ Discord Error", f"Failed: {ex}"), ephemeral=True)

    # Send alert ONLY to configured release channel (or fallback)
    release_channel: Optional[discord.TextChannel] = None
    if bot.store.release_channel_id and interaction.guild:
        ch = interaction.guild.get_channel(bot.store.release_channel_id)
        if isinstance(ch, discord.TextChannel):
            release_channel = ch
    if release_channel is None:
        # fallback to current channel
        if isinstance(interaction.channel, discord.TextChannel):
            release_channel = interaction.channel

    team_label = bot.store.format_team_label(releaser_team)
    alert_embed = embed_release_alert(guild, player, team_label, releaser_team.emoji, interaction.user)

    if release_channel:
        await release_channel.send(embed=alert_embed)

    # Ephemeral confirmation so it DOES NOT create a second public embed in the command channel
    await interaction.response.send_message(
        embed=e_success("✅ Released", f"Released {player.mention} from **{team_label}**."),
        ephemeral=True,
    )

# ----- Roster -----
@bot.tree.command(name="roster", description="View a team roster by selecting the team role.")
@app_commands.describe(team="Select the team role to view roster")
async def roster(interaction: discord.Interaction, team: discord.Role):
    if not interaction.guild:
        return await interaction.response.send_message(embed=e_error("❌ Error", "Use this in a server."), ephemeral=True)

    guild = interaction.guild
    try:
        if not guild.chunked:
            await guild.chunk(cache=True)
    except Exception:
        pass

    members = [m for m in guild.members if team in m.roles]
    count = len(members)

    team_obj = bot.store.get_team_by_role_id(team.id)
    team_emoji = team_obj.emoji if team_obj else ""
    team_name = team.name

    members_sorted = sorted(members, key=lambda m: (m.display_name or "").lower())
    lines = [f"• {m.mention}" for m in members_sorted]

    blocks = chunk_lines(lines) if lines else ["_None_"]
    total_pages = len(blocks)

    await interaction.response.send_message(
        embed=roster_embed_like_photo(guild, team_name, team_emoji, count, ROSTER_CAP, blocks[0], 1, total_pages)
    )

    for i in range(1, total_pages):
        await interaction.followup.send(
            embed=roster_embed_like_photo(guild, team_name, team_emoji, count, ROSTER_CAP, blocks[i], i + 1, total_pages)
        )

# ======================================
# RUN
# ======================================
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN (or DISCORD_TOKEN) missing in .env")

    # helpful warnings
    if not SPREADSHEET_ID:
        print("⚠️ SPREADSHEET_ID missing — /insert commands will fail until you add it to .env")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("⚠️ GOOGLE_SERVICE_ACCOUNT_JSON missing — /insert commands will fail until you add it to .env")

    bot.run(BOT_TOKEN)
