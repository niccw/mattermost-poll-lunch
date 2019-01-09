# -*- coding: utf-8 -*-
import warnings
import logging
from collections import namedtuple

from flask import Flask, request, jsonify, abort, send_from_directory
from flask_babel import Babel, gettext as tr
import flask_babel

from poll import Poll, NoMoreVotesError, InvalidPollError
from formatters import format_help, format_poll, format_user_vote
from mattermost_api import user_locale
import settings

from lunch import Lunch, InvalidLunchError
import random


app = Flask(__name__)
app.logger.propagate = True

babel = Babel(app)

try:  # pragma: no cover
    if settings.APPLY_PROXY_FIX:
        # respect X-Forwarded-Proto from proxy server (see #21)
        from werkzeug.contrib.fixers import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app)
except AttributeError:  # pragma: no cover
    pass

# Add missing settings
if not hasattr(settings, 'PUBLIC_BY_DEFAULT'):
    settings.PUBLIC_BY_DEFAULT = False
if not hasattr(settings, 'PROGRESS_BY_DEFAULT'):
    settings.PROGRESS_BY_DEFAULT = True
if not hasattr(settings, 'BARS_BY_DEFAULT'):
    settings.BARS_BY_DEFAULT = True


def parse_slash_command(command):
    """Parses a slash command for supported arguments.
    Receives the form data of the request and returns all found arguments.
    Arguments are separated by '--'.
    Supported arguments:
        - message: str
        - vote_options: list of str
        - secret: boolean (deprecated)
        - progress: boolean
        - noprogress: boolean
        - public: boolean
        - anonym: boolean
        - num_votes: int
        - bars: boolean
        - nobars: boolean
        - locale: str
    """
    args = [arg.strip() for arg in command.split('--')]
    message = args[0]
    vote_options = []
    progress = settings.PROGRESS_BY_DEFAULT
    public = settings.PUBLIC_BY_DEFAULT
    bars = settings.BARS_BY_DEFAULT
    locale = ''
    max_votes = 1

    # lunch params
    lunch = False
    lunchadd = False
    lunchrm = False
    lunchls = False

    for arg in args[1:]:
        if arg == 'secret' or arg == 'noprogress':
            progress = False
        elif arg == 'progress':
            progress = True
        elif arg == 'public':
            public = True
        elif arg == 'anonym':
            public = False
        elif arg == 'bars':
            bars = True
        elif arg == 'nobars':
            bars = False
        elif arg.startswith('locale'):
            try:
                _, locale = arg.split('=')
            except ValueError:
                pass
        elif arg.startswith('votes'):
            try:
                _, max_votes_str = arg.split('=')
                max_votes = max(1, int(max_votes_str))
            except ValueError:
                pass

        # lunch flags
        # start lunch vote
        elif arg == 'lunch': 
            lunch = True
        elif arg == 'lunchadd':
            lunchadd = True
        elif arg == 'lunchrm':
            lunchrm = True
        elif arg == 'lunchls':
            lunchls = True       
        else:
            vote_options.append(arg)

    Arguments = namedtuple('Arguments', ['message', 'vote_options',
                                         'progress', 'public', 'max_votes',
                                         'bars', 'locale','lunch','lunchadd','lunchrm','lunchls'])
    return Arguments(message, vote_options, progress,
                     public, max_votes, bars, locale,
                     lunch, lunchadd, lunchrm, lunchls)


@babel.localeselector
def get_locale():
    """Returns the locale for the current request."""
    try:
        return user_locale(request.user_id)
    except AttributeError as e:
        app.logger.warning(e)
    return "en"


@app.after_request
def log_response(response):
    """Logs the complete response for debugging."""
    if app.logger.isEnabledFor(logging.DEBUG):
        app.logger.debug('Response status: %s', response.status)
        if not response.direct_passthrough:  # excludes send_from_directory
            app.logger.debug('Response data: %s',
                             response.get_data().decode('utf-8'))
    return response


@app.route('/', methods=['GET'])
def status():
    """Returns a simple message if the server is running."""
    return tr("Poll server is running")


@app.route('/', methods=['POST'])
def poll():
    """Creates a new poll.
    Directly called by Mattermost.
    Example response data:
    ```
    {
        "response_type": "in_channel",
        "attachments": [{
            "text": "<Poll message>",
            "actions": [
            {
                "name": "<First Option> (0)",
                "integration": {
                    "context": {
                        "poll_id": "<unique_id>",
                        "vote": 0
                    },
                    "url": "http://<hostname:port>/vote"
                }
            },
            ... additional entries for all poll options ...
            {
                "name": "End Poll",
                "integration": {
                    "url": "http://<hostname:port>/end",
                    "context": {
                        "poll_id": "<unique_id>"
                    }
                }
            }],
            "fields": [
            {
                "short": false,
                "title": "",
                "value": "Number of Votes: 1"
            }]
        }]
    }
    ```
    """
    if hasattr(settings, 'MATTERMOST_TOKEN'):
        warnings.warn("MATTERMOST_TOKEN is deprecated, use MATTERMOST_TOKENS \
                      instead", category=DeprecationWarning)
        settings.MATTERMOST_TOKENS = [settings.MATTERMOST_TOKEN]
        settings.MATTERMOST_TOKEN = None

    if settings.MATTERMOST_TOKENS:
        token = request.form['token']
        if token not in settings.MATTERMOST_TOKENS:
            return jsonify({
                'response_type': 'ephemeral',
                'text': tr("The integration is not correctly set up: "
                           "Invalid token.")
            })

    if 'user_id' not in request.form:
        abort(400)
    if 'text' not in request.form:
        abort(400)
    user_id = request.form['user_id']
    request.user_id = user_id

    app.logger.debug('Received command: %s', request.form['text'])

    # the poll should have a fixed locale, otherwise it
    # changes for everyone every time someone votes
    locale = flask_babel.get_locale().language

    if request.form['text'].strip() == 'help':
        return jsonify({
            'response_type': 'ephemeral',
            'text': format_help(request.form['command'], locale)
        })

    args = parse_slash_command(request.form['text'])

    #debug
    return jsonify({'ephemeral_text': args.progress+args.lunch+args.message})

    # lunch
    # built new message and vote_options
    if args.lunch:
        lunch = Lunch()
        restaurants = lunch.read_restaurant()
        try:
            num_restaurant = int(args.message)
        except:
            return jsonify({'ephemeral_text':tr("Please provide a number.")})
        restaurants_subset = random(restaurants,num_restaurant)
        new_vote_options = restaurants_subset
        
        args_dict = args._asdict()
        args_dict['message'] = "Let's vote for lunch!"
        args_dict['vote_options'] =  new_vote_options
        
        # copy from parse_slash_command
        Arguments = namedtuple('Arguments', ['message', 'vote_options',
                                         'progress', 'public', 'max_votes',
                                         'bars', 'locale','lunch','lunchadd','lunchrm','lunchls'])
        
        args = Arguments(**args_dict)
        return jsonify({'ephemeral_text':tr("inside lunch")})

    if args.lunchadd:
        lunch = Lunch()
        flag = lunch.add_restaurant(author_id=user_id,restaurant=args.message)
        if flag:
            return jsonify({'ephemeral_text':tr("Successfully added restaurant.")})
        else:
            return jsonify({'ephemeral_text':tr("Error in adding restaurant.")})
    if args.lunchrm:
        lunch = Lunch()
        flag = lunch.rm_restaurant(args.message)
        if flag:
            return jsonify({'ephemeral_text':tr("Successfully removed restaurant.")})
        else:
            return jsonify({'ephemeral_text':tr("Error in removing restaurant.")})
    if args.lunchls:
        lunch = Lunch()
        restaurants = lunch.read_restaurant()
        restaurants_str = ",".join(restaurants)
        return jsonify({'ephemeral_text':restaurants_str})


    if not args.message:
        text = tr("**Please provide a message.**\n\n"
                  "**Usage:**\n{help}").format(
                      help=format_help(request.form['command'], locale))
        return jsonify({
            'response_type': 'ephemeral',
            'text': text
        })
    if args.public:
        if not settings.MATTERMOST_URL or not settings.MATTERMOST_PA_TOKEN:
            return jsonify({
                'response_type': 'ephemeral',
                'text': tr("Public polls are not available with the "
                           "current setup. Please check with you "
                           "system administrator.")
            })

    if args.locale:
        locale = args.locale


    poll = Poll.create(user_id,
                       message=args.message,
                       locale=locale,
                       vote_options=args.vote_options,
                       secret=not args.progress,
                       public=args.public,
                       max_votes=args.max_votes,
                       bars=args.bars)

    app.logger.info('Creating Poll: %s', poll.id)

    return jsonify(format_poll(poll))


@app.route('/vote', methods=['POST'])
def vote():
    """Places a vote for a user.
    Called through the URL in the corresponding action (see
    formatters.format_actions).
    The JSON `context` is expected to contain a valid poll_id and the
    vote_id to vote for.
    """
    json = request.get_json()
    user_id = json['user_id']
    poll_id = json['context']['poll_id']
    vote_id = json['context']['vote']
    request.user_id = user_id

    try:
        poll = Poll.load(poll_id)
    except InvalidPollError:
        return jsonify({
            'ephemeral_text': tr("This poll is not valid anymore.\n"
                                 "Sorry for the inconvenience.")
        })

    app.logger.info('Voting in poll "%s" for user "%s": %i',
                    poll_id, user_id, vote_id)
    try:
        poll.vote(user_id, vote_id)
    except NoMoreVotesError:
        return jsonify({
            'ephemeral_text': tr("You already used all your votes.\n"
                                 "Click on a vote to unselect it again.")
        })

    return jsonify({
        'update': {
            'props': format_poll(poll)
        },
        'ephemeral_text': tr("Your vote has been updated:\n{}").format(
            format_user_vote(poll, user_id))
    })


@app.route('/end', methods=['POST'])
def end_poll():
    """Ends the poll.
    Called by the 'End Poll' actions.
    Only the user that created the poll is allowed to end it.
    All other user will receive an ephemeral error message.
    """
    json = request.get_json()
    user_id = json['user_id']
    poll_id = json['context']['poll_id']
    request.user_id = user_id

    try:
        poll = Poll.load(poll_id)
    except InvalidPollError:
        return jsonify({
            'ephemeral_text': tr("This poll is not valid anymore.\n"
                                 "Sorry for the inconvenience.")
        })

    app.logger.info('Ending poll "%s"', poll_id)
    if user_id == poll.creator_id:
        # only the creator may end a poll
        poll.end()
        return jsonify({
            'update': {
                'props': format_poll(poll)
            }
        })

    return jsonify({
        'ephemeral_text': tr("You are not allowed to end this poll")
    })


@app.route('/img/<path:filename>')
def send_img(filename):
    return send_from_directory('img', filename)
