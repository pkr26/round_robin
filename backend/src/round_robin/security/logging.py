"""Access logs retain routing/status without query credentials or OIDC codes."""

import logging


class RedactAccessQueries(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            address, method, target, version, status = record.args
            if isinstance(target, str) and '?' in target:
                record.args = (address, method, target.split('?', 1)[0] + '?[redacted]', version, status)
        return True


def install_access_filter():
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(item, RedactAccessQueries) for item in logger.filters):
        logger.addFilter(RedactAccessQueries())
