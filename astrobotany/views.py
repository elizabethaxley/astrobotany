import math
import mimetypes
import os
import pathlib
import typing
from datetime import datetime, timedelta
from functools import lru_cache

import jinja2
from jetforce import JetforceApplication, Request, Response, Status
from jetforce.app.base import RateLimiter

from . import items
from .art import render_art
from .leaderboard import get_daily_leaderboard
from .models import Certificate, Inbox, ItemSlot, Message, Plant, User

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
FILES_DIR = os.path.join(os.path.dirname(__file__), "files")

template_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def datetime_format(value, fmt="%A, %B %d, %Y %-I:%M:%S %p"):
    return value.strftime(fmt)


def number_format(value):
    return "{:,}".format(value)


template_env.filters["datetime"] = datetime_format
template_env.filters["number"] = number_format

mimetypes.add_type("text/gemini", ".gmi")

password_failed_rate_limiter = RateLimiter("10/5m")
new_account_rate_limiter = RateLimiter("2/4h")
message_rate_limiter = RateLimiter("3/h")


@lru_cache(2048)
def load_session(session_id: str):
    """
    A poor man's server-side session object.

    Stores session data as a dict in memory that will be wiped on server
    restart. Mutate the dictionary to update the session. This only works
    because the server is running as a single process with shared memory.
    """
    return {}


def render_template(name: str, *args, **kwargs) -> str:
    """
    Render a gemini directory using the Jinja2 template engine.
    """
    return template_env.get_template(name).render(*args, **kwargs)


def authenticate(func: typing.Callable) -> typing.Callable:
    """
    View wrapper that handles user authentication via client certificates.
    """

    def callback(request: Request, **kwargs):

        if "REMOTE_USER" not in request.environ:
            if request.path != "/app":
                # Redirect the user to the correct "path scope" first
                return Response(Status.REDIRECT_TEMPORARY, "/app")
            else:
                msg = "Attach your client certificate to continue."
                return Response(Status.CLIENT_CERTIFICATE_REQUIRED, msg)

        if request.environ["TLS_CLIENT_AUTHORISED"]:
            # Old-style verified certificate
            serial_number = request.environ["TLS_CLIENT_SERIAL_NUMBER"]
            fingerprint = f"{serial_number:032X}"  # Convert to hex
        else:
            # New-style self signed certificate
            fingerprint = request.environ["TLS_CLIENT_HASH"]

        cert = User.login(fingerprint)
        if cert is None:
            body = render_template(
                "register.gmi",
                request=request,
                fingerprint=fingerprint,
                cert=request.environ["client_certificate"],
            )
            return Response(Status.SUCCESS, "text/gemini", body)

        request.cert = cert
        request.user = request.cert.user
        request.plant = request.user.plant
        request.session = load_session(request.user.user_id)

        request.plant.refresh()
        response = func(request, **kwargs)
        request.plant.save()
        return response

    return callback


app = JetforceApplication()


@app.route("", strict_trailing_slash=False)
def index(request):
    title_art = render_art("title.psci")
    leaderboard = get_daily_leaderboard().render(False)
    body = render_template("index.gmi", title_art=title_art, leaderboard=leaderboard)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/register-new")
def register_new(request):
    if "REMOTE_USER" not in request.environ:
        msg = "Attach your client certificate to continue."
        return Response(Status.CLIENT_CERTIFICATE_REQUIRED, msg)

    fingerprint = request.environ["TLS_CLIENT_HASH"]
    if Certificate.select().where(Certificate.fingerprint == fingerprint).exists():
        msg = "This certificate has already been linked to an account."
        return Response(Status.CERTIFICATE_NOT_AUTHORISED, msg)

    username = request.query
    if not username:
        msg = "Enter your desired username (US-ASCII characters only)"
        return Response(Status.INPUT, msg)

    if not username.isascii():
        msg = f"The username '{username}' contains invalid characters, try again"
        return Response(Status.INPUT, msg)

    if len(username) > 30:
        msg = f"The username '{username}' is too long, try again"
        return Response(Status.INPUT, msg)

    if User.select().where(User.username == username).exists():
        msg = f"the username '{username}' is already taken, try again"
        return Response(Status.INPUT, msg)

    rate_limit_resp = new_account_rate_limiter.check(request)
    if rate_limit_resp:
        return rate_limit_resp

    cert = request.environ["client_certificate"]

    user = User.initialize(username)
    Certificate.create(
        user=user,
        fingerprint=fingerprint,
        subject=cert.subject.rfc4514_string(),
        not_valid_before_utc=cert.not_valid_before,
        not_valid_after_utc=cert.not_valid_after,
    )

    return Response(Status.REDIRECT_TEMPORARY, "/app")


@app.route("/app/register-existing")
@app.route("/app/register-existing/(?P<user_id>[0-9]+)")
def register_existing(request, user_id=None):
    if "REMOTE_USER" not in request.environ:
        msg = "Attach your client certificate to continue."
        return Response(Status.CLIENT_CERTIFICATE_REQUIRED, msg)

    fingerprint = request.environ["TLS_CLIENT_HASH"]
    if Certificate.select().where(Certificate.fingerprint == fingerprint).exists():
        msg = "This certificate has already been linked to an account."
        return Response(Status.CERTIFICATE_NOT_AUTHORISED, msg)

    if user_id is None:
        username = request.query
        if not username:
            msg = "Enter your existing username"
            return Response(Status.INPUT, msg)

        try:
            user = User.select().where(User.username == username).get()
        except User.DoesNotExist:
            msg = f"No existing user was found with the name '{username}'."
            return Response(Status.BAD_REQUEST, msg)

        return Response(Status.REDIRECT_TEMPORARY, f"/app/register-existing/{user.id}")

    user = User.get_by_id(int(user_id))
    if not user.password:
        msg = "Unable to add a certificate because this account does not have a password set."
        return Response(Status.BAD_REQUEST, msg)

    password = request.query
    if not password:
        msg = "Enter your password"
        return Response(Status.SENSITIVE_INPUT, msg)

    rate_limit_resp = password_failed_rate_limiter.check(request)
    if rate_limit_resp:
        return rate_limit_resp

    if not user.check_password(password):
        msg = "Invalid password, try again"
        return Response(Status.SENSITIVE_INPUT, msg)

    cert = request.environ["client_certificate"]
    Certificate.create(
        user=user,
        fingerprint=fingerprint,
        subject=cert.subject.rfc4514_string(),
        not_valid_before_utc=cert.not_valid_before,
        not_valid_after_utc=cert.not_valid_after,
    )

    return Response(Status.REDIRECT_TEMPORARY, "/app")


@app.route("/files/(?P<path>.*)")
def files(request, path):
    url_path = pathlib.Path(path.strip("/"))

    filename = pathlib.Path(os.path.normpath(str(url_path)))
    if filename.is_absolute() or str(filename).startswith(".."):
        # Guard against breaking out of the directory
        return Response(Status.NOT_FOUND, "Not Found")

    filepath = FILES_DIR / filename
    if not filepath.exists():
        return Response(Status.NOT_FOUND, "Not Found")

    mime, encoding = mimetypes.guess_type(str(filename))
    if encoding:
        mimetype = f"{mime}; charset={encoding}"
    else:
        mimetype = mime or "application/octet-stream"

    body = filepath.read_bytes()
    return Response(Status.SUCCESS, mimetype, body)


@app.route("/app")
@authenticate
def menu(request):
    title_art = render_art("title.psci", ansi_enabled=request.cert.ansi_enabled)
    mailbox_count = request.user.inbox.where(Inbox.is_seen == False).count()
    now = datetime.now()
    body = render_template(
        "menu.gmi", request=request, title_art=title_art, mailbox_count=mailbox_count, now=now
    )
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/epilog/(?P<page>[0-9]+)")
@authenticate
def epilog(request, page):
    page = int(page)
    if page == 5:
        art_number = 4
    else:
        art_number = page
    art = render_art(f"epilog{art_number}.psci", ansi_enabled=request.cert.ansi_enabled)
    body = render_template("epilog.gmi", page=page, art=art)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/message-board")
@app.route("/app/message-board/(?P<page>[0-9]+)")
@authenticate
def message_board(request, page=1):
    page = int(page)
    paginate_by = 10
    page_count = int(math.ceil(Message.select().count() / paginate_by))
    page_count = max(page_count, 1)
    if page > page_count:
        return Response(Status.NOT_FOUND, "Invalid page number")

    items = Message.by_date().paginate(page, paginate_by)

    body = render_template(
        "message_board.gmi", request=request, items=items, page=page, page_count=page_count,
    )
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/message-board/submit")
@authenticate
def message_board_submit(request):
    if not request.query:
        return Response(Status.INPUT, "What would you like to say? ")

    rate_limit_resp = message_rate_limiter.check(request)
    if rate_limit_resp:
        return rate_limit_resp

    message = Message(user=request.user, text=request.query)
    message.save()
    return Response(Status.REDIRECT_TEMPORARY, "/app/message-board")


@app.route("/app/settings")
@authenticate
def settings(request):
    body = render_template("settings.gmi", request=request)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/settings/password")
@authenticate
def settings_password(request):
    new_password = request.session.pop("new_password", None)

    if not request.query:
        prompt = f"Enter your new password:"
        return Response(Status.SENSITIVE_INPUT, prompt)

    if not new_password:
        request.session["new_password"] = request.query
        prompt = f"Confirm your new password (enter it again):"
        return Response(Status.SENSITIVE_INPUT, prompt)

    if new_password != request.query:
        return Response(Status.BAD_REQUEST, "Passwords did not match!")

    request.user.set_password(new_password)
    request.user.save()

    message = "Password successfully updated!\n\n=>/app/settings back"
    return Response(Status.SUCCESS, "text/gemini", message)


@app.route("/app/settings/ansi_enabled")
@authenticate
def settings_ansi_enabled(request):
    if not request.query:
        prompt = f"Enable ANSI support for colors? [T]rue / [F]alse"
        return Response(Status.INPUT, prompt)

    answer = request.query.strip().lower()

    if answer in ("t", "true"):
        request.cert.ansi_enabled = True
        request.cert.save()
    elif answer in ("f", "false"):
        request.cert.ansi_enabled = False
        request.cert.save()
    else:
        return Response(Status.BAD_REQUEST, f"Invalid query value: {request.query}")

    return Response(Status.REDIRECT_TEMPORARY, "/app/settings")


@app.route("/app/settings/certificates")
@authenticate
def settings_certificates(request):
    certificates = (
        Certificate.select().where(Certificate.user == request.user).order_by(Certificate.last_seen)
    )

    body = render_template("settings_certificates.gmi", request=request, certificates=certificates,)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/settings/certificates/(?P<certificate_id>[0-9]+)/delete")
@authenticate
def settings_certificates_delete(request, certificate_id):
    cert = Certificate.get_or_none(id=certificate_id)
    if cert is None:
        msg = "Certificate not found"
        return Response(Status.BAD_REQUEST, msg)
    elif cert.user != request.user:
        msg = "Certificate not found"
        return Response(Status.BAD_REQUEST, msg)
    elif cert == request.cert:
        msg = "You cannot delete your active certificate"
        return Response(Status.BAD_REQUEST, msg)
    elif not request.query:
        msg = (
            f"Are you sure you want to delete certificate {cert.fingerprint[:10]}? "
            f'Type "confirm" to continue.'
        )
        return Response(Status.INPUT, msg)
    elif request.query.lower() != "confirm":
        return Response(Status.BAD_REQUEST, "Action cancelled")

    cert.delete_instance()
    return Response(Status.REDIRECT_TEMPORARY, "/app/settings/certificates")


@app.route("/app/store")
@authenticate
def store(request):
    coins = request.user.get_item_quantity(items.coin)
    for_sale = ItemSlot.store_view(request.user)
    body = render_template("store.gmi", request=request, coins=coins, for_sale=for_sale)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/store/purchase/(?P<item_id>[0-9]+)")
@authenticate
def store_purchase(request, item_id):
    item_id = int(item_id)
    item = items.registry.get(item_id)
    if item is None:
        return Response(Status.BAD_REQUEST, "Item was not found")

    if not item.for_sale:
        return Response(Status.BAD_REQUEST, "Item is not for sale")

    if not request.query:
        msg = f"Confirm: purchase 1 {item.name} for {item.price} coins. [Y]es/[N]o."
        return Response(Status.INPUT, msg)

    if request.query.strip().lower() in ("y", "yes"):
        if request.user.remove_item(items.coin, quantity=item.price):
            request.user.add_item(item)
        else:
            return Response(Status.BAD_REQUEST, "Insufficient funds")

    return Response(Status.REDIRECT_TEMPORARY, "/app/store")


@app.route("/app/mailbox")
@authenticate
def mailbox(request):
    messages = request.user.inbox.order_by(Inbox.id.desc())
    mailbox_art = render_art("mailbox.psci", ansi_enabled=request.cert.ansi_enabled)
    body = render_template(
        "mailbox.gmi", request=request, messages=messages, mailbox_art=mailbox_art
    )
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/mailbox/(?P<message_id>[0-9]+)")
@authenticate
def mailbox_view(request, message_id):
    message = Inbox.get_or_none(id=message_id, user_to=request.user)
    if message is None:
        return Response(Status.BAD_REQUEST, "You shouldn't be here!")

    message.is_seen = True
    message.save()

    body = render_template("mailbox_view.gmi", request=request, message=message)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/plant")
@authenticate
def plant(request):
    alert = request.session.pop("alert", None)
    if alert is None:
        alert = request.plant.get_observation(request.cert.ansi_enabled)

    body = render_template("plant.gmi", request=request, plant=request.plant, alert=alert)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/plant/water")
@authenticate
def water(request):
    request.session["alert"] = request.plant.water()
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/plant/fertilize")
@authenticate
def fertilize(request):
    request.session["alert"] = request.plant.fertilize()
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/plant/info")
@authenticate
def info(request):
    request.session["alert"] = "\n".join(
        [f"Generation: {request.plant.generation}", f"Growth Rate: {request.plant.growth_rate}"]
    )
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/plant/search")
@authenticate
def search(request):
    if request.plant.dead or request.plant.stage != 4:
        return Response(Status.BAD_REQUEST, "You shouldn't be here!")

    request.session["alert"] = request.plant.pick_petal()
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/plant/shake")
@authenticate
def shake(request):
    if request.plant.dead:
        return Response(Status.BAD_REQUEST, "You shouldn't be here!")

    request.session["alert"] = request.plant.shake()
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/plant/harvest")
@app.route("/app/plant/harvest/confirm")
@authenticate
def harvest(request):
    if not (request.plant.dead or request.plant.stage == 5):
        return Response(Status.BAD_REQUEST, "You shouldn't be here!")

    if request.path.endswith("/confirm"):
        if request.query.strip() == f"Goodbye {request.plant.name}":
            request.plant.harvest()
            return Response(Status.REDIRECT_TEMPORARY, "/app/epilog/1")
        elif request.query:
            return Response(Status.REDIRECT_TEMPORARY, "/app/plant/harvest")
        else:
            msg = f'Type "Goodbye {request.plant.name}" to send off your plant.'
            return Response(Status.INPUT, msg)

    body = render_template("plant_harvest.gmi", request=request, plant=request.plant)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/plant/name")
@authenticate
def name(request):
    if not request.query:
        return Response(Status.INPUT, "Enter a new nickname for your plant:")

    request.plant.name = request.query[:40]
    msg = f'Your plant shall henceforth be known as "{request.plant.name}".'
    request.session["alert"] = msg
    return Response(Status.REDIRECT_TEMPORARY, "/app/plant")


@app.route("/app/visit")
@authenticate
def visit(request):
    plants = (
        Plant.all_active()
        .filter(Plant.score > 0, Plant.watered_at >= datetime.now() - timedelta(days=8))
        .order_by(Plant.score.desc())
    )

    body = render_template("visit.gmi", request=request, plants=plants)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})")
@authenticate
def visit_plant(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")
    elif request.user == user:
        return Response(Status.REDIRECT_TEMPORARY, "/app/plant")

    user.plant.refresh()
    user.plant.save()

    has_postcard = request.user.get_item_quantity(items.postcard)

    alert = request.session.pop("alert", None)
    body = render_template(
        "visit_plant.gmi", request=request, plant=user.plant, alert=alert, has_postcard=has_postcard
    )
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})/water")
@authenticate
def visit_plant_water(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")
    elif request.user == user:
        return Response(Status.REDIRECT_TEMPORARY, "/app/plant")

    user.plant.refresh()
    request.session["alert"] = user.plant.water(request.user)
    user.plant.save()

    return Response(Status.REDIRECT_TEMPORARY, f"/app/visit/{user_id}")


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})/search")
@authenticate
def visit_plant_search(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")
    elif request.user == user:
        return Response(Status.REDIRECT_TEMPORARY, "/app/plant")

    if user.plant.dead or user.plant.stage_str != "flowering":
        return Response(Status.BAD_REQUEST, "You shouldn't be here!")

    user.plant.refresh()
    request.session["alert"] = user.plant.pick_petal(request.user)
    user.plant.save()

    return Response(Status.REDIRECT_TEMPORARY, f"/app/visit/{user_id}")


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})/postcard")
@authenticate
def visit_plant_postcard(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")

    subject = request.session.get("postcard_subject")
    body = render_template("visit_postcard.gmi", request=request, user=user, subject=subject)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})/postcard/subject")
@authenticate
def visit_plant_postcard_subject(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")

    if not request.query:
        return Response(Status.INPUT, "Enter the message subject (max 128 chars):")
    elif len(request.query) > 128:
        return Response(Status.BAD_REQUEST, "Subject exceeds maximum length")

    request.session["postcard_subject"] = request.query
    return Response(Status.REDIRECT_TEMPORARY, f"/app/visit/{user_id}/postcard")


@app.route("/app/visit/(?P<user_id>[0-9a-f]{32})/postcard/send")
@authenticate
def visit_plant_postcard_send(request, user_id):
    user = User.get_or_none(user_id=user_id)
    if user is None:
        return Response(Status.NOT_FOUND, "User not found")

    if not request.session.get("postcard_subject"):
        return Response(Status.BAD_REQUEST, "Postcard subject cannot be blank.")

    if not request.user.remove_item(items.postcard):
        return Response(Status.BAD_REQUEST, "You do not have any postcards in your inventory.")

    subject = request.session.pop("postcard_subject")
    body = "Hello world!"

    Inbox.create(user_from=request.user, user_to=user, subject=subject, body=body)

    request.session["alert"] = f"Your postcard to {user.username} has been mailed!"
    return Response(Status.REDIRECT_TEMPORARY, f"/app/visit/{user_id}")


@app.route("/app/inventory")
@authenticate
def inventory(request):
    inventory = sorted(request.user.inventory, key=lambda x: x.item.name)
    body = render_template("inventory.gmi", request=request, inventory=inventory)
    return Response(Status.SUCCESS, "text/gemini", body)


@app.route("/app/items/(?P<item_id>[0-9]+)")
@authenticate
def view_item(request, item_id):
    item = items.registry[int(item_id)]
    body = render_template("item.gmi", request=request, item=item)
    return Response(Status.SUCCESS, "text/gemini", body)
