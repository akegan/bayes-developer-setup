"""Integration to send Slack messages when new code reviews are sent in Reviewable."""
import collections
import enum
import json
import itertools
import os
import re
import traceback

import flask
import requests

# Map from Github users to Slack users.
_GITHUB_TO_SLACK_LOGIN = json.loads(os.getenv('GITHUB_TO_SLACK_LOGIN', '{}'))
# Channel to send unexpected error messages to (typically channel of the admin like '@florian').
_ERROR_SLACK_CHANNEL = os.getenv('ERROR_SLACK_CHANNEL')
# Set of Slack users like ['florian'] who do not want to be notified on Slack.
_DISABLED_SLACK_LOGINS = set(json.loads(os.getenv('DISABLED_SLACK_LOGINS', '[]')))
# The following variable is used for development, to check what messages are sent to all users.
_REDIRECT_ALL_SLACK_MESSAGES_TO_CHANNEL = os.getenv('REDIRECT_ALL_SLACK_MESSAGES_TO_CHANNEL')
# Token to post messages on Slack. Can be retrieved in https://api.slack.com/apps/A74SCPGAK/oauth.
_SLACK_APP_BOT_TOKEN = os.getenv('SLACK_APP_BOT_TOKEN')
_SLACK_POST_MESSAGE_ENDPOINT = 'https://slack.com/api/chat.postMessage'

app = flask.Flask(__name__)  # pylint: disable=invalid-name


@app.route('/', methods=['GET', 'POST'])
def index():
    """Health check endpoint."""
    error_message = _get_missing_env_vars_error_message()
    if error_message:
        return error_message, 500

    return '''Integration to send Reviewable updates to Slack.
        Status: ✅
        Link Github webhook to post json to /handle_github_notification'''


@app.route('/handle_github_notification', methods=['POST'])
def handle_github_notification():
    """Receives a Github webhook notification and handles it to potentially ping devs on Slack."""
    error_message = _get_missing_env_vars_error_message()
    if error_message:
        return error_message, 500

    github_event_type = flask.request.headers.get('X-GitHub-Event')
    github_notification = json.loads(flask.request.data)
    try:
        slack_messages = generate_slack_messages(github_event_type, github_notification)
        status_code = 200
    except NotEnoughDataException as err:
        # We could not figure out what pull request to send updates about, so we do a noop.
        slack_messages = {}
        status_code = 200
    except Exception as err:  # pylint: disable=broad-except
        slack_messages = {
            _ERROR_SLACK_CHANNEL:
                'Error: {}\n\n{}\n'.format(err, traceback.format_exc())
        }
        status_code = 500

    if slack_messages and _REDIRECT_ALL_SLACK_MESSAGES_TO_CHANNEL:
        # To debug the integration, send only one message with all the info to the channel used
        # to test.
        all_messages_in_one = 'Messages from Reviewable:\n' + ('\n\n'.join([
            'To {}:\n{}'.format(slack_channel, slack_message)
            for slack_channel, slack_message in slack_messages.items()
        ]) if slack_messages else 'None')
        slack_messages = {_REDIRECT_ALL_SLACK_MESSAGES_TO_CHANNEL: all_messages_in_one}

    # Ping on Slack.
    for slack_channel, slack_message in slack_messages.items():
        response = requests.post(_SLACK_POST_MESSAGE_ENDPOINT, data={
            'token': _SLACK_APP_BOT_TOKEN,
            'channel': slack_channel,
            'text': slack_message,
            'as_user': True,
        })
        if response.status_code != 200:
            return 'Error with Slack:\n{} {}'.format(response.status_code, response.text), 500
    return json.dumps(slack_messages), status_code


def _get_missing_env_vars_error_message():
    error_message = ''
    if not _GITHUB_TO_SLACK_LOGIN:
        error_message += 'Need to set up GITHUB_TO_SLACK_LOGIN as env var in the format:' +\
            '{"florianjourda": "florian"}\n'
    if not _ERROR_SLACK_CHANNEL:
        error_message += 'Need to set up ERROR_SLACK_CHANNEL as env var in the format: #general'
    if not _SLACK_APP_BOT_TOKEN:
        error_message += 'Need to set up _SLACK_APP_BOT_TOKEN as env var in the format. Get it ' +\
            'from https://api.slack.com/apps/A74SCPGAK/oauth'
    return error_message


GithubEventParams = collections.namedtuple('GithubEventParams', [
    'pull_request', 'ci_status_events', 'new_ci_status_event', 'comments', 'new_comment'])


class SetupException(Exception):
    """Exception to warn about uncomplete setup."""
    pass


class NotEnoughDataException(Exception):
    """Exception when the github notification data does not tell us what pull request it's about."""
    pass


class ExecutionException(Exception):
    """Exception to warn about communication issue with Github, Zappier or Slack."""
    pass


_GITHUB_PERSONAL_ACCESS_TOKEN = os.getenv('GITHUB_PERSONAL_ACCESS_TOKEN', '')
# TODO(pascal for florian): Please document all the regexp here, I'm a bit worried that this stops
# working if any other tool (reviewable or our own CI) change its syntax. Also document what kind
# of text they are capture if it's not obvious (like the github link regex)
_REVIEWABLE_ASSIGN_REGEX = re.compile(r'\+@([\w-]+)\b', re.MULTILINE)
_REVIEWABLE_LGTM_REGEX = re.compile(r'<img class="emoji" title=":lgtm')
_REVIEWABLE_HTML_EMOJI_REGEX = re.compile(r'<img class="emoji" title="([^"]+)"[^>]*>')
_REVIEWABLE_COMMENT_SEPARATOR = '\n\n---\n\n'
_START_OF_REVIEWABLE_COMMENT_WITHOUT_SEPARATOR_REGEX = r'^\n\n\n\nReview status:'
_START_OF_REVIEWABLE_COMMENT_WITH_SEPARATOR_REGEX = r'\n\n---\n\nReview status:'
_REVIEWABLE_UNADDRESSED_COMMENT_REGEX = re.compile(r' (\d)+ unresolved discussion')


class ReviewableEvent(enum.Enum):
    """Enum for the different type of events that happened on Reviewable."""
    ASSIGNED = 'ASSIGNED'
    COMMENTED = 'COMMENTED'
    RESPONDED = 'RESPONDED'
    APPROVED = 'APPROVED'
    CI_SUCCEEDED = 'CI_SUCCEEDED'
    CI_FAILED = 'CI_FAILED'
    CI_FIXED = 'CI_FIXED'


class CallToAction(enum.Enum):
    """Enum for the different type of action should be recommended to users on Slack."""
    REVIEW = 'REVIEW'
    SUBMIT = 'SUBMIT'
    CHECK_FEEDBACK = 'CHECK_FEEDBACK'
    CHECK_CHANGE = 'CHECK_CHANGE'
    CHECK_CI = 'CHECK_CI'
    ADDRESS_COMMENTS = 'ADDRESS_COMMENTS'
    ADD_REVIEWERS = 'ADD_REVIEWERS'
    WAIT_FOR_REVIEWERS = 'WAIT_FOR_REVIEWERS'
    WAIT_FOR_OTHER_REVIEWERS = 'WAIT_FOR_OTHER_REVIEWERS'

_EVENT_SLACK_TEMPLATES = {
    ReviewableEvent.ASSIGNED: '{who} needs your help to review {whose_change}',
    ReviewableEvent.COMMENTED: '{who} has commented on {whose_change}',
    ReviewableEvent.RESPONDED: '{who} has responsed to comments on {whose_change}',
    ReviewableEvent.APPROVED: '{who} has approved {whose_change}',
    ReviewableEvent.CI_SUCCEEDED: 'Continuous integration tests succeeded for {whose_change}',
    ReviewableEvent.CI_FAILED: '❗️ Continuous integration tests failed for {whose_change}',
    ReviewableEvent.CI_FIXED: '✅ Continuous integration tests fixed for {whose_change}',
}

_CALL_TO_ACTION_TEMPLATES = {
    CallToAction.REVIEW: "Let's <{code_review_url}|check this code>!",
    CallToAction.SUBMIT: "Let's `git submit`!",
    CallToAction.CHECK_FEEDBACK: "Let's <{code_review_url}|check their feedback>!",
    CallToAction.CHECK_CHANGE: "Let's <{code_review_url}|check what they have changed>!",
    CallToAction.CHECK_CI: "Let's <{ci_url}|check what the problem is>.",
    CallToAction.ADDRESS_COMMENTS:
        'But you still have <{code_review_url}|{unaddressed_comments_string} to address>.',
    CallToAction.ADD_REVIEWERS: "Let's <{code_review_url}|add reviewers>!",
    CallToAction.WAIT_FOR_REVIEWERS: 'The reviewers have now been asked to review.',
    CallToAction.WAIT_FOR_OTHER_REVIEWERS: 'You now need to wait for the other reviewers.',
}


def generate_slack_messages(github_event_type, github_notification):
    """Generate all the messages to send on Slack to respond to a Github notification."""
    if github_event_type == 'issue_comment':
        github_event_params = _get_all_resources_for_issue_comment_event(github_notification)
    elif github_event_type == 'status':
        if github_notification['branches'][0]['name'] == 'master':
            # TODO(florian): Also deal with notifications from tags.
            # We ignore notifications on master.
            # TODO(florian): Add message when breaking CI on master.
            return {}

        if github_notification['context'].startswith('code-review/reviewable'):
            # We use only comments instead of code-review status to signal which users have given
            # an LGTM.
            return {}
        github_event_params = _get_all_resources_for_ci_status_event(github_notification)
    else:
        # We deal only with new comments and new CI/code review status notifications.
        return {}
    return _generate_slack_messages_for_new_status_or_comment(**github_event_params._asdict())


def _get_all_resources_for_issue_comment_event(github_notification):
    """Fetch on Github API resources that are missing in the 'issue_comment' notification."""
    # TODO(florian): we use issue in our code but we actually want a pull request. GitHub just
    # happens to use them a bit one for the other, but here our code should be clearer.
    issue = github_notification['issue']
    if 'pull_request' not in issue:
        # This can happen when editing directly the source code on Github without going through a
        # pull request, or when pushing a branch from the commandline without creating a
        # pull request.
        raise NotEnoughDataException('No pull request')
    pull_request_url = issue['pull_request']['url']
    pull_request = _get_github_api_ressource(pull_request_url)
    ci_status_events = _get_github_api_ressource(pull_request['statuses_url'])
    new_ci_status_event = None
    comments = _get_github_api_ressource(pull_request['comments_url'])
    # Get the version of new_comment from the API instead of from the github notification.
    new_comment = next(
        comment for comment in comments
        if comment['id'] == github_notification['comment']['id'])
    return GithubEventParams(
        pull_request=pull_request,
        ci_status_events=ci_status_events,
        new_ci_status_event=new_ci_status_event,
        comments=comments,
        new_comment=new_comment)


def _get_all_resources_for_ci_status_event(github_notification):
    """Fetch on Github API resources that are missing in the 'status' notification."""
    # Unfortunately 'status' event don't contain 'issue' data, so we need to fetch it.
    new_status_event = github_notification
    if not new_status_event['context'].startswith('ci/circleci'):
        raise ExecutionException(
            "Does not support '{}' status context".format(new_status_event['context']))
    new_ci_status_event = new_status_event

    filter_for_branch = '?base=master&head={}:{}'.format(
        new_ci_status_event['repository']['owner']['login'],
        new_ci_status_event['branches'][0]['name'])
    pull_request_url = new_ci_status_event['repository']['pulls_url'].replace(
        '{/number}', filter_for_branch)
    pull_requests = _get_github_api_ressource(pull_request_url)
    if len(pull_requests) != 1:
        raise NotEnoughDataException('Did not find a single pull_request: {}'.format(
            pull_requests))
    pull_request = pull_requests[0]

    ci_status_events = _get_github_api_ressource(pull_request['statuses_url'])
    # Get the version of new_ci_status_event from the API instead of from the github notification.
    new_ci_status_event = next(
        event for event in ci_status_events
        if event['id'] == new_ci_status_event['id'])
    comments = _get_github_api_ressource(pull_request['comments_url'])
    new_comment = None
    return GithubEventParams(
        pull_request=pull_request,
        ci_status_events=ci_status_events,
        new_ci_status_event=new_ci_status_event,
        comments=comments,
        new_comment=new_comment)


def _generate_slack_messages_for_new_status_or_comment(
        pull_request, ci_status_events, new_ci_status_event, comments, new_comment):
    """Prepare all data we need to decide what messages to generate."""
    # Make sure we did not get statuses and comments that arrived after the new status or comment.
    # Note: new_comment is included in comments, and new_ci_status_event in ci_status_events.
    ci_status_events = _get_dicts_before(ci_status_events, new_ci_status_event)
    comments = _get_dicts_before(comments, new_comment)
    reviewee = pull_request['user']['login']

    assignees = {assignee['login'] for assignee in pull_request['assignees']}
    # Remove the owner from the assignees if for some reason they self-assigned. This will simplify
    # our already complex logic later.
    assignees.discard(reviewee)

    new_assignees = _REVIEWABLE_ASSIGN_REGEX.findall(new_comment['body']) if new_comment else {}

    commentors = {comment['user']['login'] for comment in comments}
    new_commentor = new_comment['user']['login'] if new_comment else None

    new_ci_state = new_ci_status_event['state'] if new_ci_status_event else None
    ci_state, ci_url, previous_not_pending_ci_state = _get_ci_states(ci_status_events)

    lgtm_givers = _get_lgtm_givers(comments)
    new_lgtm_givers = _get_lgtm_givers([new_comment] if new_comment else [])
    # We don't count LGTM from user that were not assignees.
    remaining_assignees = assignees - lgtm_givers

    unaddressed_comment_count = _get_unaddressed_comment_count(comments)
    can_submit = (
        assignees and not remaining_assignees and
        not unaddressed_comment_count and ci_state == 'success')

    if new_commentor:
        from_user = new_commentor
    elif new_ci_state:
        from_user = reviewee
    else:
        from_user = new_ci_status_event['creator']['login']
    slack_messages = {}

    def add_slack_message(to_user, event, call_to_action):
        """Helper function to reduce boiler plate when calling _generate_slack_message."""
        slack_messages.update(_generate_slack_message(
            from_user=from_user,
            event=event,
            to_user=to_user,
            call_to_action=call_to_action,
            pull_request=pull_request,
            ci_url=ci_url,
            new_comment=new_comment,
            unaddressed_comment_count=unaddressed_comment_count))

    # Here is all the logic tree about what message to send to whom.
    if not ci_state or ci_state == 'pending':
        # Don't ping anyone if CI is not done!
        return {}

    if new_ci_state == 'failure':
        # CI tests just failed, warn the reviewee.
        add_slack_message(reviewee, ReviewableEvent.CI_FAILED, CallToAction.CHECK_CI)
        return slack_messages

    if new_ci_state == 'success':
        # The CI is now ready so we should ask the remaining assignees to review it.
        for assignee in remaining_assignees:
            add_slack_message(assignee, ReviewableEvent.ASSIGNED, CallToAction.REVIEW)
        # If the CI if fixed we should tell the reviewee.
        event = (
            ReviewableEvent.CI_FIXED if previous_not_pending_ci_state == 'failure' else
            ReviewableEvent.CI_SUCCEEDED)
        call_to_action = (
            CallToAction.SUBMIT if can_submit else
            CallToAction.WAIT_FOR_REVIEWERS if remaining_assignees else
            CallToAction.ADD_REVIEWERS if not assignees else
            # The logic brings us here is unaddressed_comment_count is not zero.
            CallToAction.ADDRESS_COMMENTS)
        if (event == ReviewableEvent.CI_SUCCEEDED and
                call_to_action == CallToAction.WAIT_FOR_REVIEWERS):
            # Don't tell the reviewee to wait for reviewers the first time the CI succeeded, as this
            # is the default expected situation.
            return slack_messages
        add_slack_message(reviewee, event, call_to_action)
        return slack_messages

    if new_assignees:
        # We have new assignees to ask to review the change.
        for assignee in new_assignees:
            add_slack_message(assignee, ReviewableEvent.ASSIGNED, CallToAction.REVIEW)
        return slack_messages

    # New comment is just a new comment.
    if new_commentor != reviewee:
        # A reviewer gave some feedback to the pull_request owner.
        if new_lgtm_givers:
            # The reviewer gave an LGTM.
            if can_submit:
                add_slack_message(reviewee, ReviewableEvent.APPROVED, CallToAction.SUBMIT)
            elif unaddressed_comment_count:
                # But there are still comments to address.
                add_slack_message(reviewee, ReviewableEvent.APPROVED, CallToAction.ADDRESS_COMMENTS)
            else:
                # But there are still other reviewers to wait for.
                add_slack_message(
                    reviewee, ReviewableEvent.APPROVED, CallToAction.WAIT_FOR_OTHER_REVIEWERS)
        elif new_comment:
            # The reviewer gave some comments.
            add_slack_message(reviewee, ReviewableEvent.COMMENTED, CallToAction.CHECK_FEEDBACK)
        return slack_messages

    # The pull request owner wrote some feedback.
    for assignee in remaining_assignees:
        if assignee in commentors:
            # If the assignee had written some comment before, it is likely the pull request
            # owner just responded to them.
            add_slack_message(assignee, ReviewableEvent.RESPONDED,
                              CallToAction.CHECK_FEEDBACK)
        else:
            # The assignee had not contributed to the review yet, so it's time to do it.
            add_slack_message(assignee, ReviewableEvent.COMMENTED, CallToAction.REVIEW)
    if unaddressed_comment_count:
            # But there are still some comments they should address.
        add_slack_message(reviewee, ReviewableEvent.COMMENTED,
                          CallToAction.ADDRESS_COMMENTS)

    return slack_messages


def _get_dicts_before(dicts, reference_dict):
    if not reference_dict:
        return dicts
    dicts_before = [
        a_dict for a_dict in dicts
        if a_dict['updated_at'] <= reference_dict['updated_at']
    ]
    return dicts_before


def _get_ci_states(ci_status_events):
    """Return the current state, previous not 'pending' state and ci_url of the pull request.

    Return None if none found.
    """
    # Check the format of statuses here: https://developer.github.com/v3/repos/statuses/
    # TODO(add more doc about the different event formats)
    ci_status_events = sorted(ci_status_events, key=lambda event: event['context'])
    events_by_context = itertools.groupby(ci_status_events, key=lambda event: event['context'])
    ci_state = None
    ci_url = None
    previous_not_pending_ci_state = None
    for context, context_events in events_by_context:
        # TODO(florian): improve to work with multiple CI.
        if not context.startswith('ci/'):
            continue
        orderered_events = sorted(context_events, key=lambda event: event['updated_at'])
        ci_status_event = orderered_events[-1]
        ci_url = ci_status_event['target_url']
        ci_state = ci_status_event['state']
        previous_events = orderered_events[:-1]
        previous_not_pending_events = [
            event for event in previous_events
            if event['state'] != 'pending']
        if previous_not_pending_events:
            previous_not_pending_ci_state = previous_not_pending_events[-1]['state']
    return ci_state, ci_url, previous_not_pending_ci_state


def _get_lgtm_givers(comments):
    """Return which users have given an LGTM in the previous comments."""
    lgtm_givers = {
        comment['user']['login'] for comment in comments
        if _REVIEWABLE_LGTM_REGEX.match(comment['body'])
    }
    return lgtm_givers


def _generate_slack_message(
        from_user, event, to_user, call_to_action, pull_request,
        ci_url, new_comment, unaddressed_comment_count):
    slack_login = _get_slack_login(to_user)
    if slack_login in _DISABLED_SLACK_LOGINS:
        return {}
    slack_channel = '@' + slack_login
    repository_name = pull_request['head']['repo']['full_name']
    code_review_url = 'https://reviewable.io/reviews/{}/{}'.format(
        repository_name, pull_request['number'])
    event_slack_string = _generate_event_slack_string(
        from_user, event, to_user, pull_request, code_review_url)
    comment_recap = _generate_comment_recap(new_comment) if new_comment else ''
    call_to_action_string = _generate_call_to_action_slack_string(
        call_to_action, code_review_url, ci_url, unaddressed_comment_count)
    slack_message = '_{}:_\n{}{}'.format(event_slack_string, comment_recap, call_to_action_string)
    return {slack_channel: slack_message}


def _get_slack_login(github_login):
    """Return the slack login of a github user."""
    slack_login = _GITHUB_TO_SLACK_LOGIN.get(github_login)
    if slack_login is None:
        raise SetupException("Need to add Github user '{}' to GITHUB_TO_SLACK_LOGIN".format(
            github_login))
    return slack_login


def _generate_event_slack_string(
        from_user, event, to_user, pull_request, code_review_url):
    if from_user == to_user:
        who = 'You'
    else:
        who = '@' + _get_slack_login(from_user)

    reviewee = pull_request['user']['login']
    if reviewee == to_user:
        whose = 'your'
    elif reviewee == from_user:
        whose = 'their'
    else:
        whose = '@' + _get_slack_login(reviewee) + "'s"
    title_without_trailing_dot = str.rstrip(pull_request['title'], '.')
    whose_change = '{} change <{}|{}>'.format(whose, code_review_url, title_without_trailing_dot)

    event_slack_string = _EVENT_SLACK_TEMPLATES[event].format(who=who, whose_change=whose_change)
    return event_slack_string


def _generate_call_to_action_slack_string(
        call_to_action, code_review_url, ci_url, unaddressed_comment_count):
    unaddressed_comments_string = '{} comment{}'.format(
        unaddressed_comment_count, '' if unaddressed_comment_count == 1 else 's')
    call_to_action_slack_string = _CALL_TO_ACTION_TEMPLATES[call_to_action].format(
        code_review_url=code_review_url, ci_url=ci_url,
        unaddressed_comments_string=unaddressed_comments_string)
    return call_to_action_slack_string


def _generate_comment_recap(new_comment):
    comment_recap = ''
    main_comment, inline_comment_count, unused_unaddressed_comment_count =\
        _get_comment_parts(new_comment['body'])
    if inline_comment_count:
        inline_comment_count_sentence = '{} inline comment{}'.format(
            inline_comment_count, '' if inline_comment_count == 1 else 's')
        comment_recap = main_comment + '\nand ' + inline_comment_count_sentence\
            if main_comment else inline_comment_count_sentence
    else:
        comment_recap = main_comment
    return comment_recap + '\n'


def _get_comment_parts(comment_body):
    # If there is no main comment the format of the comment is a bit weird at the beginning,
    # and needs to be normalized to allow us to split the different part more easily.
    comment_body = re.sub(
        _START_OF_REVIEWABLE_COMMENT_WITHOUT_SEPARATOR_REGEX,
        _START_OF_REVIEWABLE_COMMENT_WITH_SEPARATOR_REGEX, comment_body)
    comment_parts = comment_body.split(_REVIEWABLE_COMMENT_SEPARATOR)
    # main_comment will be '' if the substitution happened above.
    main_comment = _replace_emoji_image_by_emoji_name(comment_parts[0])
    if len(comment_parts) == 1:
        # This is a comment written directly from Github, it doesn't have the parts of Reviewable.
        inline_comment_count = 0
        # TODO(florian): figure out the real unaddressed_comment_count in this case.
        unaddressed_comment_count = 0
    else:
        review_status = comment_parts[1]
        match = _REVIEWABLE_UNADDRESSED_COMMENT_REGEX.search(review_status)
        unaddressed_comment_count = int(match[1]) if match else 0
        inline_comments = comment_parts[2:-1]
        inline_comment_count = len(inline_comments)
    return main_comment, inline_comment_count, unaddressed_comment_count


def _replace_emoji_image_by_emoji_name(html_text):
    return _REVIEWABLE_HTML_EMOJI_REGEX.sub(r'\1', html_text)


def _get_github_api_ressource(ressource_url):
    """Calls Github API to retrieve resource state."""
    if not _GITHUB_PERSONAL_ACCESS_TOKEN:
        raise SetupException('Need to define _GITHUB_PERSONAL_ACCESS_TOKEN env variable.')
    auth = tuple(_GITHUB_PERSONAL_ACCESS_TOKEN.split(':'))
    # TODO(florian): Get items on page > 1 if necessary.
    per_page = ('&' if '?' in ressource_url else '?') + 'per_page=100'
    response = requests.get(ressource_url + per_page, auth=auth)
    if response.status_code != 200:
        raise ExecutionException('Could not retrieve object from Github API:\n{}\n{}: {}'.format(
            ressource_url, response.status_code, response.text
        ))
    return response.json()


def _get_unaddressed_comment_count(comments):
    """Tells how many comments are still to be adressed by the review owner."""
    if not comments:
        return 0
    last_comment = comments[-1]
    unused_main_comment, unused_inline_comment_count, unaddressed_comment_count =\
        _get_comment_parts(last_comment['body'])
    return unaddressed_comment_count


# We only need this for local development.
if __name__ == '__main__':
    app.run(debug=True)
