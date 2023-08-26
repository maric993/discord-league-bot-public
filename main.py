import os
import datetime
import d2api
import time
import sys
import asyncio
import yaml

from discord import Intents, Message, Member, Embed
from discord.ext import commands
from discord.ext.commands import Bot, Context

from dotenv import load_dotenv
from typing import Any, List, Union
from more_itertools import chunked

from discord_db import execute_function_no_return, execute_function_single_row_return, execute_insert_and_return_id, execute_function_with_return
from models.console import ConsoleView
from models.errors import DataBaseErrorNonModified
from models.player import Player
from utility import get_random_password, balanced_shuffle, calculate_elo, split_digits
from models.draft import DraftView

env_path = '.dev.env' if len(sys.argv) > 1 and sys.argv[1] == 'dev' else '.env'
load_dotenv(os.path.join(os.path.abspath(os.path.dirname(__file__)), env_path))
yaml_path = 'league_settings_dev.yaml' if len(
    sys.argv) > 1 and sys.argv[1] == 'dev' else 'league_settings.yaml'

with open(yaml_path) as f:
    league_settings = yaml.load(f, Loader=yaml.FullLoader)
    LEAGUE_ID = league_settings.get('league_id', 0)
    GAME_NAME = league_settings.get('game_name_prefix', '') + ' #'
    ADMIN_ROLE = league_settings.get('discord_league_admin_role_name', '')
    STARTING_MMR = league_settings.get('league_starting_mmr', 1000)
    LOBBY_SIZE = league_settings.get('lobby_size', 10)

TOKEN: str = os.getenv('TOKEN', '')
STEAM_API_TOKEN = os.getenv('STEAM_API_TOKEN', '')
DEFAULT_CHANNEL_ID: Union[str, None] = os.getenv('DEFAULT_CHANNEL_ID', None)
LEADERBOARD_CHANNEL_ID: Union[str, None] = os.getenv(
    'LEADERBOARD_CHANNEL_ID', None)
ADMIN_CHANNEL_ID: Union[str, None] = os.getenv('ADMIN_CHANNEL_ID', None)
DRAFT_CHANNEL_ID: Union[str, None] = os.getenv('DRAFT_CHANNEL_ID', None)
COMMAND_CHANNEL_ID : Union[str, None]= os.getenv('COMMAND_CHANNEL_ID', None)
CONSOLE_CHANNEL_ID : Union[str, None]= os.getenv('CONSOLE_CHANNEL_ID', None)


PREFIX = '!'
SKIP_GAMES = []  # Used when the same league was used for testing or previous season

RENDER = {'leaderboard': False, 'queue': False, 'queue_draft': False}
AUTO_SCORING_IN_PROGRESS = False

async def update_embed_loop(callback, flag):
    global RENDER
    while True:
        if RENDER[flag]:
            await callback()
            RENDER[flag] = False
        await asyncio.sleep(5)

async def _look_for_timeout_games():
    global RENDER
    while True:
        try:
            games = execute_function_with_return('get_game_where_status_timeout')
        except ValueError:
            games = []
        for game in games:
            try:
                players = execute_function_with_return('get_players_arrived', game['id'])
            except ValueError:
                players = []
            if game['type'] == 'DRAFT':
                RENDER['queue_draft'] = True
                returning_players = [Player(player['id']) for player in players if player['id'] not in [player.id for player in bot.sigedUpDraftPlayerPool]]
                bot.sigedUpDraftPlayerPool = returning_players + bot.sigedUpDraftPlayerPool
                await _check_pool_size_and_start_draft()
            else:
                RENDER['queue'] = True
                returning_players = [Player(player['id']) for player in players if player['id'] not in [player.id for player in bot.sigedUpPlayerPool]]
                bot.sigedUpPlayerPool = returning_players + bot.sigedUpPlayerPool
                await _check_pool_size_and_start()
            execute_function_no_return('set_game_status_aborted', game['id'])
        await asyncio.sleep(5)
        

intents = Intents.default().all()
api = d2api.APIWrapper(STEAM_API_TOKEN)

bot = Bot(
    command_prefix=commands.when_mentioned_or(PREFIX),
    intents=intents,
    help_command=None,
)

bot.sigedUpPlayerPool: List[Member] = []  # type: ignore
bot.sigedUpDraftPlayerPool: List[Member] = []  # type: ignore


@bot.event
async def on_ready():
    global RENDER

    if not DEFAULT_CHANNEL_ID or \
    not LEADERBOARD_CHANNEL_ID or \
    not ADMIN_CHANNEL_ID or \
    not DRAFT_CHANNEL_ID or \
    not COMMAND_CHANNEL_ID or \
    not CONSOLE_CHANNEL_ID:
        _log('You need to set all channel ids in .env file')
        exit(1)

    await bot.tree.sync()

    bot.default_channel = bot.get_channel(int(DEFAULT_CHANNEL_ID))
    bot.leaderboard_channel = bot.get_channel(int(LEADERBOARD_CHANNEL_ID))
    bot.admin_channel = bot.get_channel(int(ADMIN_CHANNEL_ID))
    bot.draft_channel = bot.get_channel(int(DRAFT_CHANNEL_ID))
    bot.command_channel = bot.get_channel(int(COMMAND_CHANNEL_ID))
    bot.console_channel = bot.get_channel(int(CONSOLE_CHANNEL_ID))

    await _rerender_queue_console_if_needed(True)
    await _rerender_leaderboard_if_needed(True)
    asyncio.ensure_future(update_embed_loop(_update_leaderboard, 'leaderboard'))
    asyncio.ensure_future(update_embed_loop(_update_queue, 'queue'))
    asyncio.ensure_future(update_embed_loop(_update_draft_queue, 'queue_draft'))
    asyncio.ensure_future(_look_for_timeout_games())
    _log(f'Logged in as {bot.user}')


@bot.event
async def on_message(message: Message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx: Context, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.reply('Invalid command used.', mention_author=True, delete_after=10)
        return
    if isinstance(error, commands.MissingRole):
        await ctx.reply('You dont have permission to use this command.', mention_author=True, delete_after=10)
        return
    _log(error)


@bot.hybrid_command(name="help", description="Show commands")
async def help(ctx: Context):
    embed = Embed(title="Nemoguca Liga Bot",
                  description="Commands", color=0xeee657)
    embed.add_field(name="/help", value="Show this message", inline=False)
    embed.add_field(name="/stats", value="Show my stats", inline=False)
    embed.add_field(name="/signup", value="Signup for the game", inline=False)
    embed.add_field(name="/signupdraft",
                    value="Signup for the game", inline=False)
    embed.add_field(name="/leave", value="Leave the queue", inline=False)
    embed.add_field(name="/autoscore",
                    value="Attempt to score a game via Steam API(do not spam)", inline=False)
    embed.add_field(name="/preferredrole", value="Set your preferred rolse eg., 123 or 34 or 5 or 345 etc.", inline=False)
    if ADMIN_ROLE in [role.id for role in ctx.message.author.roles]:
        embed.add_field(name="/vouch @DiscordUser SteamId",
                        value="Vouch for a player", inline=False)
        embed.add_field(name="/score MatchNumber dire | radiant",
                        value="Score a match", inline=False)
        embed.add_field(name="/rehost MatchNumber",
                        value="Try to rehost a game", inline=False)
        embed.add_field(name="/clearqueue",
                        value="Clear the queue", inline=False)
        embed.add_field(name="/cleardraftqueue",
                        value="Clear the queue", inline=False)
        embed.add_field(name="/cancelgame MatchNumber", value="Cancel a game", inline=False)
        embed.add_field(name="/markcaptain @DiscordUser", value="Mark a player as captain", inline=False)

    await ctx.send(embed=embed, delete_after=60)


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("vouch", description="Vouch for a player")
async def vouch(ctx: Context, discord_id: str, steam_id: str):
    discord_id = discord_id.replace('\\', '').replace('<', '').replace('>', '').replace(
        '@', '').replace('!', '').replace('#', '').replace('&', '')  # <@337347092139737099>
    try:
        execute_function_single_row_return('get_player_id', discord_id)
        await ctx.reply('Player <@{0}> has already been vouched for'.format(discord_id), delete_after=10)
    except ValueError:
        execute_function_no_return(
            'add_player', discord_id, steam_id, STARTING_MMR)
        await ctx.reply('<@{0}> has been vouched'.format(discord_id))


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("score", description="Score a match")
async def score(ctx: Context, game_id: str, score: str, steam_match_id: str):
    global RENDER
    if score not in ['dire', 'radiant']:
        #i dont trust the users to type 0 for radiant and 1 for dire
        await ctx.reply('Score needs to be the name of the winning team(radiant or dire)', delete_after=10)
    else:
        result = 0 if score == 'radiant' else 1
        try:
            game = execute_function_single_row_return('get_game', game_id)
        except ValueError:
            await ctx.reply('Game with id {} does not exist'.format(game_id), delete_after=10)
            return
        if game['status'] == 'OVER':
            await ctx.reply('Game already scored', delete_after=10)
            return
        
        execute_function_no_return(
            'score_game', game_id, result, steam_match_id)
        
        players = execute_function_with_return(
            'get_all_players_from_game', game_id)

        team_one = [player['mmr'] for player in players if player['team'] == 0]
        team_two = [player['mmr'] for player in players if player['team'] == 1]
        team_one_avg_mmr = round(
            sum(team_one)/len(team_one)) if len(team_one) > 0 else 0
        team_two_avg_mmr = round(
            sum(team_two)/len(team_two)) if len(team_two) > 0 else 0
        if len(team_one) > 0 and len(team_two) > 0:
            elo_change = calculate_elo(
                team_one_avg_mmr, team_two_avg_mmr, 1 if result == 0 else -1)
        else:
            elo_change = 25  # only applys to testing when the queue size is one
        for player in players:
            if player['team'] == result:  # if players has the same team as the one that won
                execute_function_no_return('update_player_mmr_won', player['id'], elo_change)
            else:
                execute_function_no_return('update_player_mmr_lost', player['id'], elo_change)

        await ctx.reply('Game scored, {} won game {}'.format(score, game_id))
        RENDER['leaderboard'] = True


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("rehost", description="Try to rehost a game")
async def rehost(ctx: Context, game_id: str):
    global RENDER
    try:
        execute_function_single_row_return('get_game', game_id)
    except ValueError:
        await ctx.reply('Game with id {} does not exist'.format(game_id), delete_after=10)
        return
    players = execute_function_with_return('get_all_players_from_game', game_id)
    for player in players:
        if player['discord_id'] in [player.id for player in bot.sigedUpPlayerPool]:
            player_to_remove = next(member for member in bot.sigedUpPlayerPool if member.id == player['discord_id'] )
            bot.sigedUpPlayerPool.remove(player_to_remove)
            RENDER['queue'] = True
        if player['discord_id'] in [player.id for player in bot.sigedUpDraftPlayerPool]:
            player_to_remove = next(member for member in bot.sigedUpDraftPlayerPool if member.id == player['discord_id'])
            bot.sigedUpDraftPlayerPool.remove(player_to_remove)
            RENDER['queue_draft'] = True
    execute_function_no_return('reset_all_players_arrived', game_id)
    execute_function_no_return('set_game_status_rehost', game_id)
    await ctx.reply('Game rehosted')


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("clearqueue", description="Clear the queue")
async def clear_queue(ctx: Context):
    global RENDER
    bot.sigedUpPlayerPool = []
    await ctx.reply('Queue cleared by <@{0}>'.format(ctx.message.author.id))
    RENDER['queue'] = True


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("cleardraftqueue", description="Clear the queue")
async def clear_queue(ctx: Context):
    global RENDER
    bot.sigedUpDraftPlayerPool = []
    await ctx.reply('Queue cleared by <@{0}>'.format(ctx.message.author.id))
    RENDER['queue_draft'] = True


@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("cancelgame", description="Cancel a game")
async def cancel_game(ctx: Context, game_id: str):
    global RENDER
    game = execute_function_single_row_return('get_game', game_id)
    if game['status'] in ['OVER', 'ABORTED']:
        await ctx.reply('Game already scored or aborted', delete_after=10)
        return
    try:
        players = execute_function_with_return('get_players_arrived', game_id)
        if game['type'] == 'DRAFT':
            returning_players = [Player(player['id']) for player in players if player['id'] not in [player.id for player in bot.sigedUpDraftPlayerPool]]
            bot.sigedUpDraftPlayerPool = returning_players + bot.sigedUpDraftPlayerPool
            RENDER['queue_draft'] = True
            await _check_pool_size_and_start_draft(ctx)
        else:
            returning_players = [Player(player['id']) for player in players if player['id'] not in [player.id for player in bot.sigedUpPlayerPool]]
            bot.sigedUpPlayerPool = returning_players + bot.sigedUpPlayerPool
            RENDER['queue'] = True
            await _check_pool_size_and_start(ctx)
    except ValueError:
        pass
    execute_function_no_return('set_game_status_cancel', game_id)
    await ctx.reply('Game canceled')

@commands.has_role(ADMIN_ROLE)
@bot.hybrid_command("markcaptain", description="Cancel a game")
async def cancel_game(ctx: Context, discord_id: str):
    discord_id = discord_id.replace('\\', '').replace('<', '').replace('>', '').replace(
        '@', '').replace('!', '').replace('#', '').replace('&', '')  # <@337347092139737099>
    try:
        player_id = execute_function_single_row_return('get_player_id', discord_id)['id']
    except ValueError:
        await ctx.reply('Player needs to be vouched before becoming a captain.', mention_author=True, delete_after=10)
    execute_function_no_return('set_player_captain', player_id)
    await ctx.reply('Player <@{}> marked as captain'.format(discord_id))
    


@bot.hybrid_command("autoscore", description="Attempt to score a game")
async def autoscore(ctx: Context):
    global AUTO_SCORING_IN_PROGRESS 
    if LEAGUE_ID == 0:
        await ctx.reply('League id not set. autoscore can not be used', delete_after=10)
        return
    matches = api.get_match_history(league_id=LEAGUE_ID)
    if len(matches['matches']) == 0:
        await ctx.reply('No matches found in a league with id {0}'.format(LEAGUE_ID), delete_after=10)
        return
    try:
        active_games = execute_function_with_return('get_active_games')
    except ValueError:
        await ctx.reply('No games in progress', delete_after=10)
        return
    
    if AUTO_SCORING_IN_PROGRESS:
        await ctx.reply('Autoscoring already in progress', delete_after=10)
        return
    
    AUTO_SCORING_IN_PROGRESS  = True
    active_game_players_dict = {}
    for game_players in active_games:
        players_in_active_game = execute_function_with_return(
            'get_all_players_from_game', game_players['id'])
        active_game_players_dict[game_players['id']] = [
            (player['steam_id'], player['team']) for player in players_in_active_game]
    try:
        scored_games = execute_function_with_return(
            'get_scored_games_with_steam_match_id')
    except ValueError:
        scored_games = []
        pass

    all_ready_scored_games = [game['steam_match_id'] for game in scored_games]
    skip_games = SKIP_GAMES + all_ready_scored_games

    for match in matches['matches']:
        if match['match_id'] in skip_games:
            matches['matches'].remove(match)

    for match in matches['matches']:
        if len(active_game_players_dict) == 0:
            break

        steam_match_id = match['match_id']
        players = []
        for player in match['players']:
            team = 0 if player['side'] == 'radiant' else 1
            steam_id = player['steam_account']['id64']
            #bots can be in game but dont have steam id
            if steam_id is not None:
                players.append((steam_id, team))

        matching_games = []
        for id, game_players in active_game_players_dict.items():
            if set(game_players) == set(players):
                matching_games.append(id)


        if len(matching_games) == 0:
            await ctx.reply(f'Could not get game with steam_match_id: {steam_match_id}')
            continue

        if len(matching_games) > 1:
            await ctx.reply(f'More than one game found for steam_match_id: {steam_match_id}')
            for game_id in matching_games:
                active_game_players_dict.pop(game_id)
            continue

        #len(matching_games) == 1
        game_id = matching_games[0]
        active_game_players_dict.pop(game_id)         

        winner = api.get_match_details(steam_match_id)['winner']

        await score(ctx, game_id, winner, steam_match_id)
        await asyncio.sleep(5)
        AUTO_SCORING_IN_PROGRESS  = False


@bot.hybrid_command("signup", description="Signup for the game")
async def signup(ctx: Context):
    global RENDER
    author: Member = ctx.message.author  # type: ignore
    try:
        execute_function_single_row_return('get_player_id', author.id)
    except ValueError:
        await ctx.reply('You need to signup for the leage', mention_author=True, delete_after=10)
        return
    if author not in bot.sigedUpPlayerPool:  # type: ignore
        bot.sigedUpPlayerPool.append(author)  # type: ignore
        await ctx.reply('You successfully signed up for a game', mention_author=True, delete_after=10)
        RENDER['queue'] = True
        await _check_pool_size_and_start(ctx)
    else:
        await ctx.reply('You have already signed up for the game', mention_author=True, delete_after=10)


@bot.hybrid_command("signupdraft", description="Signup for the game")
async def signup(ctx: Context):
    global RENDER
    author: Member = ctx.message.author  # type: ignore
    try:
        execute_function_single_row_return('get_player_id', author.id)
    except ValueError:
        await ctx.reply('You need to signup for the leage', mention_author=True, delete_after=10)
        return
    if author not in bot.sigedUpDraftPlayerPool:  # type: ignore
        bot.sigedUpDraftPlayerPool.append(author)  # type: ignore
        await ctx.reply('You successfully signed up for a game', mention_author=True, delete_after=10)
        RENDER['queue_draft'] = True
        await _check_pool_size_and_start_draft(ctx)
    else:
        await ctx.reply('You have already signed up for the game', mention_author=True, delete_after=10)


@bot.hybrid_command("stats", description="Show my stats")
async def stats(ctx: Context):
    author: Member = ctx.message.author  # type: ignore
    try:
        player_id = execute_function_single_row_return(
            'get_player_id', author.id)['id']
    except ValueError:
        await ctx.reply('You need to signup for the leage', mention_author=True, delete_after=10)
        return
    games_played = execute_function_single_row_return(
        'get_if_player_played_game', player_id)['played']
    if games_played == 0:
        await ctx.reply('You have not played any games', mention_author=True, delete_after=10)
        return

    player_rank = execute_function_single_row_return(
        'get_player_rank', player_id)
    wins_and_losses = execute_function_single_row_return(
        'get_players_wins_and_losses', player_id)

    embed = Embed(title="Stats", description="Your stats", color=0xeee657)
    embed.add_field(
        name=f"Player", value=f'''<@{player_rank['discord_id']}>''', inline=True)
    embed.add_field(name=f"Rank", value=player_rank['rank'], inline=True)
    embed.add_field(name=f"MMR", value=player_rank['mmr'], inline=True)
    embed.add_field(name=f"Wins", value=wins_and_losses['wins'], inline=True)
    embed.add_field(
        name=f"Losses", value=wins_and_losses['losses'], inline=True)
    await ctx.reply(embed=embed, delete_after=20)


@bot.hybrid_command("leave", description="Leave the queue")
async def leave(ctx: Context):
    global RENDER
    author: Member = ctx.message.author  # type: ignore
    player_to_remove_from_queue = next((member for member in bot.sigedUpPlayerPool if member.id == author.id ), None)
    player_to_remove_from_draft_queue = next((member for member in bot.sigedUpDraftPlayerPool if member.id == author.id ), None)
    if  player_to_remove_from_queue is None and  player_to_remove_from_draft_queue is None:  # type: ignore
        await ctx.reply('You are not in the queue', mention_author=True, delete_after=10)
        return
    if player_to_remove_from_queue:
        bot.sigedUpPlayerPool.remove(player_to_remove_from_queue)
    if player_to_remove_from_draft_queue:
        bot.sigedUpDraftPlayerPool.remove(player_to_remove_from_draft_queue)
    await ctx.reply('You left the queue', mention_author=True, delete_after=10)
    RENDER['queue'] = True
    RENDER['queue_draft'] = True


@bot.hybrid_command("preferredrole", description="Set your prefred role")
async def prefred_role(ctx: Context, roles: int):
    author: Member = ctx.message.author  # type: ignore
    try:
        player_id = execute_function_single_row_return('get_player_id', author.id)['id']
    except ValueError:
        await ctx.reply('You need to signup for the leage', mention_author=True, delete_after=10)
        return
    try:
        execute_function_no_return('delete_player_roles', player_id)
    except DataBaseErrorNonModified:
        pass
    if roles == 0:
        await ctx.reply('You removed your roles', mention_author=True, delete_after=10)
        return
    #split digits
    roles = list(set(split_digits(roles)))
    if len(roles) > 5 or any(role not in [1,2,3,4,5] for role in roles):
        await ctx.reply('Roles need to be between 1 and 5', mention_author=True, delete_after=10)
        return

    for role in roles:
        execute_function_no_return('set_player_role', player_id, role)
    await ctx.reply('Your prefred roles have been set', mention_author=True, delete_after=10)

async def _create_game(ctx: Context, platers_for_lobby: List[Member]):
    global RENDER

    players_for_lobby, lobby_name, lobby_password = await _get_game_args(platers_for_lobby)

    await _send_game_embed(ctx, players_for_lobby, lobby_name)
    await _send_game_name_and_password(players_for_lobby, lobby_name, lobby_password)


async def _get_game_args(players: List[Member]):
    players_for_lobby = _get_players_from_db(players)
    balanced_shuffle(players_for_lobby) #will add team(0 or 1) to players in players_for_lobby

    game_id = execute_insert_and_return_id('add_game', 'NORMAL')
    lobby_name = GAME_NAME + str(game_id)
    lobby_password = get_random_password()

    execute_function_no_return('add_game_args', game_id, lobby_name, lobby_password)
    for player in players_for_lobby:
        execute_function_no_return('add_player_to_game', game_id, player['id'], player['team'])
        
    _log(f'Creating game #{game_id}')
    return players_for_lobby, lobby_name, lobby_password


async def _create_a_draft_game(ctx, players_for_lobby: List[Any]):
    game_id = execute_insert_and_return_id('add_game', 'DRAFT')
    _log(f'Creating game #{game_id}')
    lobby_name = GAME_NAME + str(game_id)
    lobby_password = get_random_password()

    execute_function_no_return(
       'add_game_args', game_id, lobby_name, lobby_password)
    for player in players_for_lobby:
       execute_function_no_return(
           'add_player_to_game', game_id, player['id'], player['team'])

    await _send_game_embed(ctx, players_for_lobby, lobby_name)
    await _send_game_name_and_password(players_for_lobby, lobby_name, lobby_password)


async def _send_game_name_and_password(players, lobbyname, lobby_password):
    await bot.admin_channel.send(f"Game hosted.\nLobby name: {lobbyname}\nPassword: {lobby_password}")
    for player in players:
        discord_user = await bot.fetch_user(player['discord_id'])
        await discord_user.send(f"Game name: {lobbyname}\nPassword: {lobby_password}\nYou are playing as: {'**Radiant**' if player['team'] == 0 else '**Dire**'}")


async def _create_game_embed(players, lobyname) -> Embed:
    radiant_str, radiant_avg_mmr = _calculate_team_stats(players, 0)
    dire_str, dire_avg_mmr = _calculate_team_stats(players, 1)

    embed = Embed(title=lobyname,
                  description="Teams", color=0x00ff00)
    embed.add_field(
        name=f"Radiant: {radiant_avg_mmr}", value=radiant_str, inline=False)
    embed.add_field(name=f"Dire: {dire_avg_mmr}",
                    value=dire_str, inline=False)
    return embed


def _get_players_from_db(players: List[Member]) -> List[Any]:
    loby_players = []
    for p in players:
        player = execute_function_single_row_return(
            'get_player', p.id)  # player.id in this case is discord_id
        loby_players.append(player)
    return loby_players


def _players_list(players):
    players_str = ''
    for player in players:
        players_str += '<@' + \
            str(player['discord_id']) + '> ' + str(player['mmr']) + '\n'
    return players_str


def _calculate_team_stats(players, team_id):
    team_players = [player for player in players if player['team'] == team_id]
    team_str = _players_list(team_players)
    team_size = len(team_players)
    avg_mmr = sum([player['mmr'] for player in team_players]) / \
        team_size if team_size > 0 else 0
    return team_str, avg_mmr


async def _send_game_embed(ctx, players_for_lobby, lobby_name):
    embed = await _create_game_embed(players_for_lobby, lobby_name)
    await bot.default_channel.send(embed=embed)  # type: ignore
    if ctx:
        await ctx.reply(f'''Game created, chekout <#{bot.default_channel.id}>''', mention_author=True, delete_after=10)  # type: ignore
    else:
        await bot.command_channel.send(f'''Game created, chekout <#{bot.default_channel.id}>''', delete_after=10)  # type: ignore


async def _create_leaderboard_embed():
    try:
        standings = execute_function_with_return('get_leaderboards')
    except ValueError:
        standings = execute_function_with_return('get_all_players') #new season no players to load
    embed = Embed(title="Standings",
                  description="", color=0xeee657)

    player_str = ''
    mmr_str = ''

    embed.add_field(name=f"Player", value=player_str, inline=True)
    embed.add_field(name=f"MMR", value=mmr_str, inline=True)
    chunks = list(chunked(standings, 20))
    rank = 0
    for chunk in chunks[0:7]:
        embed.add_field(name="                            ", value="", inline=False)
        player_str = ""
        mmr_str = ""
        separator = 0 #every 10 players add a separator, a new line in the field
        for player in chunk:
            player_str += f'{rank+1} <@' + str(player['discord_id']) + '>' + '\n'
            mmr_str += str(player['mmr']) + '\n'
            rank += 1
            separator += 1
            if separator == 10:
                player_str += "\n"
                mmr_str += "\n"
        embed.add_field(name="", value=player_str, inline=True)
        embed.add_field(name="", value=mmr_str, inline=True)


    return embed

async def _check_pool_size_and_start(ctx: Context):
    global RENDER
    if len(bot.sigedUpPlayerPool) >= LOBBY_SIZE:  # type: ignore
        for player in bot.sigedUpPlayerPool[:LOBBY_SIZE]:
            if player in bot.sigedUpDraftPlayerPool:
                bot.sigedUpDraftPlayerPool.remove(player)

        platers_for_lobby = bot.sigedUpPlayerPool[:LOBBY_SIZE]
        bot.sigedUpPlayerPool = bot.sigedUpPlayerPool[LOBBY_SIZE:]

        RENDER['queue'] = True
        RENDER['queue_draft'] = True

        await _create_game(ctx, platers_for_lobby)

async def _check_pool_size_and_start_draft(ctx: Context = None):
    global RENDER
    if len(bot.sigedUpDraftPlayerPool) >= LOBBY_SIZE:  # type: ignore
        draft_view = DraftView(bot,ctx, bot.sigedUpDraftPlayerPool[:LOBBY_SIZE], _create_a_draft_game)
        draft_view_content = draft_view.create_view_embed()

        for player in bot.sigedUpDraftPlayerPool[:LOBBY_SIZE]:
            if player in bot.sigedUpPlayerPool:
                bot.sigedUpPlayerPool.remove(player)
        bot.sigedUpDraftPlayerPool = bot.sigedUpDraftPlayerPool[LOBBY_SIZE:]

        RENDER['queue'] = True
        RENDER['queue_draft'] = True

        await bot.draft_channel.send(embed=draft_view_content, view=draft_view)
        if ctx:
            await ctx.reply(f'''Game created, chekout <#{bot.draft_channel.id}>''', delete_after=10)  # type: ignore
        else:
            await bot.command_channel.send(f'''Game created, chekout <#{bot.draft_channel.id}>''', delete_after=10)

def _create_queue_embed():
    embed = Embed(title="Players in queue",
                  description=f"Currently {len(bot.sigedUpPlayerPool)}/{LOBBY_SIZE} players looking for a game.", color=0xeee657)  # type: ignore

    player_str = ''
    for player in bot.sigedUpPlayerPool:  # type: ignore
        player_str += '<@' + str(player.id) + '>' + '\n'

    embed.add_field(name=f"Player", value=player_str, inline=True)
    return embed


def _create_draft_queue_embed():
    embed = Embed(title=f"Players in draft queue",
                  description=f"Currently {len(bot.sigedUpDraftPlayerPool)}/{LOBBY_SIZE} players looking for a game.", color=0xeee657)  # type: ignore

    player_str = ''
    for player in bot.sigedUpDraftPlayerPool:  # type: ignore
        player_str += '<@' + str(player.id) + '>' + '\n'

    embed.add_field(name=f"Player", value=player_str, inline=True)
    return embed


def _log(message, level='INFO    '):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{timestamp}] [{level}] {message}')


async def _update_leaderboard():
     messages = await _rerender_leaderboard_if_needed()
     leaderboard_message = messages[0]
     leaderboard_embed = await _create_leaderboard_embed()
     await leaderboard_message.edit(embed=leaderboard_embed)


async def _update_queue():
    messages = await _rerender_queue_console_if_needed()
    queue_message = messages[0]
    queue_embed = _create_queue_embed()
    await queue_message.edit(embed=queue_embed)


async def _update_draft_queue():
    messages = await _rerender_queue_console_if_needed()
    draft_queue_message = messages[1]
    draft_queue_embed = _create_draft_queue_embed()
    await draft_queue_message.edit(embed=draft_queue_embed)


async def _update_console():
     embed = Embed(title="Console")
     embed.add_field(name="Interact with the bot", value="Use the buttons below to interact with the bot")
     return embed

async def _rerender_queue_console_if_needed(on_start=False):
    messages = [message async for message in bot.console_channel.history(oldest_first=True)]
    if len(messages) < 3 or on_start:
        for message in messages:
            await message.delete()
        draft_queue_embed = _create_draft_queue_embed()
        queue_embed = _create_queue_embed()
        console_embed = await _update_console()
        draft_queue = await bot.console_channel.send(embed=draft_queue_embed)
        queue = await bot.console_channel.send(embed=queue_embed)
        console = await bot.console_channel.send(embed=console_embed, view=ConsoleView(bot, RENDER, _check_pool_size_and_start, _check_pool_size_and_start_draft))
        return [draft_queue, queue, console]
    return messages

async def _rerender_leaderboard_if_needed(on_start=False):
    messages = [message async for message in bot.leaderboard_channel.history(oldest_first=True)]
    if len(messages) != 1 or on_start:
        for message in messages:
            await message.delete()
        leaderboard_embed = await _create_leaderboard_embed()
        leaderboard = await bot.leaderboard_channel.send(embed=leaderboard_embed)
        return [leaderboard]
    return messages


bot.run(TOKEN)
