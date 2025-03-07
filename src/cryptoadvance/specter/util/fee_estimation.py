import logging

logger = logging.getLogger(__name__)


def get_fees(specter, config):
    if specter.fee_estimator == "mempool":
        # Try first with Tor hidden service
        try:
            requests_session = specter.requests_session(force_tor=True)
            return requests_session.get(
                f"{config['EXPLORERS_LIST']['MEMPOOL_SPACE_ONION']['url']}api/v1/fees/recommended"
            ).json()
            return
        except Exception as e:
            # Try without Tor if failed, or fall back to Bitcoin Core if only Tor is on
            logger.warning(
                f"Failed to fetch fee estimation from mempool.space Tor hidden service. Using Bitcoin Core instead. Error: {e}"
            )  # Falling back to Bitcoin Core
            if not specter.only_tor:
                try:
                    requests_session = specter.requests_session(force_tor=False)
                    return requests_session.get(
                        f"{config['EXPLORERS_LIST']['MEMPOOL_SPACE']['url']}api/v1/fees/recommended"
                    ).json()
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch fee estimation from mempool.space. Using Bitcoin Core instead. Error: {e}"
                    )  # Falling back to Bitcoin Core
    elif specter.fee_estimator == "custom":
        try:
            if specter.config["fee_estimator_custom_url"].endswith("/"):
                custom_url = (
                    specter.config["fee_estimator_custom_url"]
                    + "api/v1/fees/recommended"
                )
            else:
                custom_url = (
                    specter.config["fee_estimator_custom_url"]
                    + "/api/v1/fees/recommended"
                )
            requests_session = specter.requests_session(
                force_tor=".onion/" in custom_url
            )
            return requests_session.get(custom_url).json()
        except Exception as e:
            logger.warning(
                f"Failed to fetch fee estimation from custom provider. Using Bitcoin Core instead. Error: {e}"
            )  # Falling back to Bitcoin Core

    return {
        "fastestFee": int((float(specter.estimatesmartfee(1)["feerate"]) / 1000) * 1e8),
        "halfHourFee": int(
            (float(specter.estimatesmartfee(3)["feerate"]) / 1000) * 1e8
        ),
        "hourFee": int((float(specter.estimatesmartfee(6)["feerate"]) / 1000) * 1e8),
        "minimumFee": int(
            (float(specter.estimatesmartfee(20)["feerate"]) / 1000) * 1e8
        ),
    }
