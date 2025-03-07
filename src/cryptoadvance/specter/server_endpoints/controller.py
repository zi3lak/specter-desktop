import random, traceback, socket, threading, os
from datetime import datetime
from binascii import unhexlify
from flask import make_response
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import MethodNotAllowed
from flask import render_template, request, redirect, url_for, flash, Markup
from flask_login import login_required, current_user
from ..helpers import (
    generate_mnemonic,
    notify_upgrade,
)
from ..specter import Specter
from ..specter_error import SpecterError, ExtProcTimeoutException
from ..util.tor import start_hidden_service, stop_hidden_services
from ..util.bitcoind_setup_tasks import (
    setup_bitcoind_thread,
    setup_bitcoind_directory_thread,
)
from ..util.tor_setup_tasks import (
    setup_tor_thread,
)
from pathlib import Path

env_path = Path(".") / ".flaskenv"
from dotenv import load_dotenv

load_dotenv(env_path)

from flask import current_app as app
from .filters import filters_bp

app.register_blueprint(filters_bp)

# Setup specter endpoints
from .auth import auth_endpoint
from .devices import devices_endpoint
from .price import price_endpoint
from .settings import settings_endpoint
from .wallets import wallets_endpoint
from ..rpc import RpcError

app.register_blueprint(auth_endpoint, url_prefix="/auth")
app.register_blueprint(devices_endpoint, url_prefix="/devices")
app.register_blueprint(price_endpoint, url_prefix="/price")
app.register_blueprint(settings_endpoint, url_prefix="/settings")
app.register_blueprint(wallets_endpoint, url_prefix="/wallets")

rand = random.randint(0, 1e32)  # to force style refresh

########## exception handlers ##############
@app.errorhandler(RpcError)
def server_rpc_error(rpce):
    """ Specific EpecterErrors get passed on to the User as flash """
    message = f"BitcoinCore RpcError: {str(se)}"
    if rpce.error_code == -18:  # RPC_WALLET_NOT_FOUND
        message = message + "Specter reloaded all Wallets, please try again.", "error"
    try:
        app.specter.wallet_manager.update()
    except SpecterError as se:
        flash(str(se), "error")
    return redirect(url_for("about"))


@app.errorhandler(SpecterError)
def server_specter_error(se):
    """ Specific EpecterErrors get passed on to the User as flash """
    flash(str(se), "error")
    try:
        app.specter.wallet_manager.update()
    except SpecterError as se:
        flash(str(se), "error")
    return redirect(url_for("about"))


@app.errorhandler(Exception)
def server_error(e):
    """ Unspecific Exceptions get a 500 Error-Page """
    # if rpc is not available
    if app.specter.rpc is None or not app.specter.rpc.test_connection():
        # make sure specter knows that rpc is not there
        app.specter.check()
    app.logger.error("Uncaught exception: %s" % e)
    trace = traceback.format_exc()
    app.logger.error(trace)
    return render_template("500.jinja", error=e, traceback=trace), 500


@app.errorhandler(ExtProcTimeoutException)
def server_error_timeout(e):
    """ Unspecific Exceptions get a 500 Error-Page """
    # if rpc is not available
    if app.specter.rpc is None or not app.specter.rpc.test_connection():
        # make sure specter knows that rpc is not there
        app.specter.check()
    app.logger.error("ExternalProcessTimeoutException: %s" % e)
    flash(
        "Bitcoin Core is not coming up in time. Maybe it's just slow but please check the logs below",
        "warn",
    )
    return redirect(url_for("settings_endpoint.bitcoin_core_internal_logs"))


@app.errorhandler(CSRFError)
def server_error_csrf(e):
    """CSRF token missing. Most likely session expired.
    If persisting after refresh this could mean the front-end
    is not sending the CSRF token properly in some form"""
    app.logger.error("CSRF Exception: %s" % e)
    trace = traceback.format_exc()
    app.logger.error(trace)
    flash("Session expired. Please refresh and try again.", "error")
    return redirect(request.url)


@app.errorhandler(MethodNotAllowed)
def server_error_405(e):
    """ 405 method not allowed. Token might have expired."""
    app.logger.error("405 MethodNotAllowed Exception: %s" % e)
    trace = traceback.format_exc()
    app.logger.error(trace)
    flash("Session expired. Please refresh and try again.", "error")
    return redirect(request.url)


########## on every request ###############
@app.before_request
def selfcheck():
    """check status before every request"""
    if app.specter.rpc is not None:
        type(app.specter.rpc).counter = 0
        if not app.specter.chain:
            app.specter.check()
    if app.config.get("LOGIN_DISABLED"):
        app.login("admin")


########## template injections #############
@app.context_processor
def inject_debug():
    """ Can be used in all jinja2 templates """
    return dict(debug=app.config["DEBUG"])


################ Specter global routes ####################
@app.route("/")
@login_required
def index():
    if request.args.get("mode"):
        if request.args.get("mode") == "remote":
            pass
    notify_upgrade(app, flash)
    if len(app.specter.wallet_manager.wallets) > 0:
        if len(app.specter.wallet_manager.wallets) > 1:
            return redirect(url_for("wallets_endpoint.wallets_overview"))
        return redirect(
            url_for(
                "wallets_endpoint.wallet",
                wallet_alias=app.specter.wallet_manager.wallets[
                    app.specter.wallet_manager.wallets_names[0]
                ].alias,
            )
        )

    return redirect("about")


@app.route("/about", methods=["GET", "POST"])
@login_required
def about():
    notify_upgrade(app, flash)
    if request.method == "POST":
        action = request.form["action"]
        if action == "cancelsetup":
            app.specter.setup_status["stage"] = 0
            app.specter.reset_setup("bitcoind")
            app.specter.reset_setup("torbrowser")

    return render_template("base.jinja", specter=app.specter, rand=rand)


@app.route("/node_setup_wizard/", defaults={"step": 0}, methods=["GET", "POST"])
@app.route("/node_setup_wizard/<step>", methods=["GET", "POST"])
@login_required
def node_setup_wizard(step):
    if app.specter.setup_status["stage"] == 0:
        app.specter.reset_setup("bitcoind")
        app.specter.reset_setup("torbrowser")
    if app.specter.setup_status["stage"] == 5:
        app.specter.setup_status["stage"] = 0

    return render_template(
        "node_setup_wizard.jinja", step=step, specter=app.specter, rand=rand
    )


@app.route("/setup_bitcoind", methods=["POST"])
@login_required
def setup_bitcoind():
    app.specter.config["internal_node"]["datadir"] = request.form.get(
        "bitcoin_core_datadir", app.specter.config["internal_node"]["datadir"]
    )
    app.specter._save()
    if os.path.exists(app.specter.config["internal_node"]["datadir"]):
        if request.form["override_data_folder"] != "true":
            return {"error": "data folder already exists"}
    if (
        not os.path.isfile(app.specter.bitcoind_path)
        and app.specter.setup_status["bitcoind"]["stage_progress"] == -1
    ):
        app.specter.update_setup_status("bitcoind", "STARTING_SETUP")
        t = threading.Thread(
            target=setup_bitcoind_thread,
            args=(app.specter, app.config["INTERNAL_BITCOIND_VERSION"]),
        )
        t.start()
    elif os.path.isfile(app.specter.bitcoind_path):
        return {"error": "bitcoind already installed"}
    elif app.specter.setup_status["bitcoind"]["stage_progress"] != -1:
        return {"error": "bitcoind installation is still under progress"}
    return {"success": "Starting Bitcoin Core setup!"}


@app.route("/setup_bitcoind_directory", methods=["POST"])
@login_required
def setup_bitcoind_directory():
    if (
        os.path.isfile(app.specter.bitcoind_path)
        and app.specter.setup_status["bitcoind"]["stage_progress"] == -1
    ):
        app.specter.setup_status["stage"] = 4  # TODO: Structure stages with enum
        app.specter.update_setup_status("bitcoind", "STARTING_SETUP")
        quicksync = request.form["quicksync"] == "true"
        pruned = request.form["nodetype"] == "pruned"
        t = threading.Thread(
            target=setup_bitcoind_directory_thread,
            args=(app.specter, quicksync, pruned),
        )
        t.start()
    elif not os.path.isfile(app.specter.bitcoind_path):
        return {"error": "bitcoind in not installed but required for this step"}
    elif app.specter.setup_status["bitcoind"]["stage_progress"] != -1:
        return {"error": "bitcoind installation is still under progress"}
    return {"success": "Starting Bitcoin Core setup!"}


@app.route("/setup_tor", methods=["POST"])
@login_required
def setup_tor():
    if (
        not os.path.isfile(app.specter.torbrowser_path)
        and app.specter.setup_status["torbrowser"]["stage_progress"] == -1
    ):
        app.specter.setup_status["torbrowser"]["stage_progress"] = 0
        app.specter.setup_status["torbrowser"]["error"] = ""
        app.specter._save()
        t = threading.Thread(
            target=setup_tor_thread,
            args=(app.specter,),
        )
        t.start()
    elif os.path.isfile(app.specter.torbrowser_path):
        return {"error": "tor is already installed"}
    elif app.specter.setup_status["torbrowser"]["stage_progress"] != -1:
        return {"error": "Tor installation is still under progress"}
    return {"success": "Starting Tor setup!"}


@app.route("/get_node_setup_status")
@login_required
@app.csrf.exempt
def get_node_setup_status():
    return app.specter.get_setup_status("bitcoind")


@app.route("/get_tor_setup_status")
@login_required
@app.csrf.exempt
def get_tor_setup_status():
    return app.specter.get_setup_status("torbrowser")


# TODO: Move all these below to REST API

################ Utils ####################


@app.route("/wallets_loading/", methods=["GET", "POST"])
@login_required
def wallets_loading():
    return {
        "is_loading": app.specter.wallet_manager.is_loading,
        "loaded_wallets": [
            app.specter.wallet_manager.wallets[wallet].alias
            for wallet in app.specter.wallet_manager.wallets
        ],
        "failed_load_wallets": [
            wallet["alias"] for wallet in app.specter.wallet_manager.failed_load_wallets
        ],
    }


@app.route("/generatemnemonic/", methods=["GET", "POST"])
@login_required
def generatemnemonic():
    return {"mnemonic": generate_mnemonic(strength=int(request.form["strength"]))}


################ RPC data utils ####################
@app.route("/get_fee/<blocks>")
@login_required
def fees(blocks):
    return app.specter.estimatesmartfee(int(blocks))


@app.route("/get_txout_set_info")
@login_required
@app.csrf.exempt
def txout_set_info():
    res = app.specter.rpc.gettxoutsetinfo()
    return res


@app.route("/get_scantxoutset_status")
@login_required
@app.csrf.exempt
def get_scantxoutset_status():
    status = app.specter.rpc.scantxoutset("status", [])
    app.specter.info["utxorescan"] = status.get("progress", None) if status else None
    if app.specter.info["utxorescan"] is None:
        app.specter.utxorescanwallet = None
    return {
        "active": app.specter.info["utxorescan"] is not None,
        "progress": app.specter.info["utxorescan"],
    }


@app.route("/bitcoin.pdf")
@login_required
def get_whitepaper():
    if app.specter.chain == "main":
        if not app.specter.info["pruned"]:
            raw_tx = app.specter.rpc.getrawtransaction(
                "54e48e5f5c656b26c3bca14a8c95aa583d07ebe84dde3b7dd4a78f4e4186e713",
                False,
                "00000000000000ecbbff6bafb7efa2f7df05b227d5c73dca8f2635af32a2e949",
            )
            outputs = raw_tx.split("0100000000000000")
            pdf = ""
            for output in outputs[1:-2]:
                cur = 6
                pdf += output[cur : cur + 130]
                cur += 132
                pdf += output[cur : cur + 130]
                cur += 132
                pdf += output[cur : cur + 130]
            pdf += outputs[-2][6:-4]
        else:
            outputs_prun = app.specter.rpc.multi(
                [
                    (
                        "gettxout",
                        "54e48e5f5c656b26c3bca14a8c95aa583d07ebe84dde3b7dd4a78f4e4186e713",
                        i,
                    )
                    for i in range(0, 946)
                ]
            )
            pdf = ""
            for output in outputs_prun[:-1]:
                cur = 4
                pdf += output["result"]["scriptPubKey"]["hex"][cur : cur + 130]
                cur += 132
                pdf += output["result"]["scriptPubKey"]["hex"][cur : cur + 130]
                cur += 132
                pdf += output["result"]["scriptPubKey"]["hex"][cur : cur + 130]
            pdf += outputs_prun[-1]["result"]["scriptPubKey"]["hex"][4:-4]
        res = make_response(unhexlify(pdf[16:-16]))
        res.headers.set("Content-Disposition", "attachment")
        res.headers.set("Content-Type", "application/pdf")
        return res
    else:
        return render_template(
            "500.jinja",
            error="You need a mainnet node to retrieve the whitepaper. Check your node configurations.",
        )
