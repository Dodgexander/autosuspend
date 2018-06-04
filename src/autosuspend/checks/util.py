from .. import ConfigurationError, TemporaryCheckError


class CommandMixin(object):
    """Mixin for configuring checks based on external commands."""

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name, config['command'].strip())
        except KeyError:
            raise ConfigurationError('Missing command specification')

    def __init__(self, command):
        self._command = command


class XPathMixin(object):

    @classmethod
    def create(cls, name, config, **kwargs):
        from lxml import etree
        try:
            xpath = config['xpath'].strip()
            # validate the expression
            try:
                etree.fromstring('<a></a>').xpath(xpath)
            except etree.XPathEvalError:
                raise ConfigurationError('Invalid xpath expression: ' + xpath)
            timeout = config.getint('timeout', fallback=5)
            return cls(name, xpath, config['url'], timeout, **kwargs)
        except ValueError as error:
            raise ConfigurationError('Configuration error ' + str(error))
        except KeyError as error:
            raise ConfigurationError('No ' + str(error) +
                                     ' entry defined for the XPath check')

    def __init__(self, xpath, url, timeout):
        self._xpath = xpath
        self._url = url
        self._timeout = timeout

    def evaluate(self):
        import requests
        import requests.exceptions
        from lxml import etree

        try:
            reply = requests.get(self._url, timeout=self._timeout).content
            root = etree.fromstring(reply)
            return root.xpath(self._xpath)
        except requests.exceptions.RequestException as error:
            raise TemporaryCheckError(error)
        except etree.XMLSyntaxError as error:
            raise TemporaryCheckError(error)
