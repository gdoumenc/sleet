import logging
import os
from http.client import BadStatusLine
from xmlrpc import client

from pyexpat import ExpatError
from chalice import NotFoundError, BadRequestError

from .. import Blueprint
from ..coworks import TechMicroService, ChaliceViewError


class OdooMicroService(TechMicroService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.url = self.db = self.username = self.password = self.models_url = self.api_uid = self.logger = None

    def get_model(self, model: str, searched_field: str, searched_value=None, fields=None, ensure_one=False):
        """Returns the list of objects or the object which searched_field is equal to the searched_value."""
        if not searched_value:
            return BadRequestError(f"{searched_field} not defined.")
        results = self.search(model, [[(searched_field, '=', searched_value)]], fields=fields if fields else [])

        if ensure_one:
            return self._ensure_one(results)

        if not results:
            raise NotFoundError()
        return results

    def put_model(self, model, data=None):
        """Delete the object of the model referenced by this id."""
        if 'id' in data:
            return self.write(model, data)
        return self.create(model, data)

    def delete_model(self, model, _id, dry=False):
        """Delete the object of the model referenced by this id."""
        return self.execute_kw(model, 'unlink', [[int(_id)]], dry=dry)

    def get_field(self, model, searched_field, searched_value, returned_field='id'):
        """Returns the value of the object which searched_field is equal to the searched_value."""
        value = self.get_model(model, searched_field, searched_value, fields=[returned_field], ensure_one=True)
        return value[returned_field]

    def get_id(self, model, searched_field, searched_value):
        """Returns the id of the object which searched_field is equal to the searched_value."""
        return self.get_field(model, searched_field, searched_value)

    def connect(self, url=None, database=None, username=None, password=None):

        # initialize connection informations
        self.url = url or os.getenv('ODOO_URL')
        if not self.url:
            raise EnvironmentError('ODOO_URL must be set before anything else!')
        self.db = database or os.getenv('ODOO_DB')
        if not self.db:
            raise EnvironmentError('ODOO_DB must be set before anything else!')
        self.username = username or os.getenv('ODOO_USERNAME')
        if not self.username:
            raise EnvironmentError('ODOO_USERNAME must be set before anything else!')
        self.password = password or os.getenv('ODOO_PASSWORD')
        if not self.password:
            raise EnvironmentError('ODOO_PASSWORD must be set before anything else!')

        self.logger = logging.getLogger('odoo')

        try:
            # initialize xml connection to odoo
            common = client.ServerProxy(f'{self.url}/xmlrpc/2/common')
            self.api_uid = common.authenticate(self.db, self.username, self.password, {})
            if not self.api_uid:
                raise Exception(f'Odoo connection parameters are wrong')
            self.models_url = f'{self.url}/xmlrpc/2/object'
        except Exception:
            raise Exception(f'Odoo interface variables wrongly defined.')

    def execute_kw(self, model: str, method: str, *args, dry=False):
        try:
            if not model:
                raise ChaliceViewError("Model undefined")

            if not self.api_uid:
                self.connect()
            self.logger.info(f'Execute_kw : {model}, {method}, {list(args)}')
            if dry:
                return

            with client.ServerProxy(self.models_url, allow_none=True) as models:
                return models.execute_kw(self.db, self.api_uid, self.password, model, method, *args)
        except (BadStatusLine, ExpatError):
            self.logger.debug(f'Retry execute_kw : {model} {method} {args}')
            with client.ServerProxy(self.models_url) as models:
                return models.execute_kw(self.db, self.api_uid, self.password, model, method, *args)
        except Exception as e:
            raise ChaliceViewError(str(e))

    def search(self, model, filters: list, fields=None, offset=None, limit=None, order=None) -> list:
        options = {}
        if fields:
            options["fields"] = fields if type(fields) is list else [fields]
        options.setdefault("limit", offset if offset else 0)
        options.setdefault("limit", limit if limit else 50)
        options.setdefault("order", order if order else 'id asc')
        return self.execute_kw(model, 'search_read', filters, options)

    def create(self, model, data: dict, dry=False):
        return self.execute_kw(model, 'create', [self._replace_tuple(data)], dry=dry)

    def write(self, model, data: dict, dry=False):
        _id = data.pop('id')
        return self.execute_kw(model, 'write', [[_id], self._replace_tuple(data)], dry=dry)

    @staticmethod
    def _ensure_one(results) -> dict:
        """Ensure only only one in the result list and returns it."""
        if len(results) == 0:
            raise NotFoundError(f"No object found.")
        if len(results) > 1:
            raise NotFoundError(
                f"More than one object ({len(results)}) founds : ids={[o.get('id') for o in results]}")
        return results[0]

    def _replace_tuple(self, struct: dict) -> dict:
        """For data from JSON, tuple are defined with key surronded by paranthesis."""
        for k, value in struct.items():
            if isinstance(value, dict):
                self._replace_tuple(value)
            else:
                if k.startswith('(') and type(value) is list:
                    del struct[k]
                    struct[k[1:-1]] = [tuple(v) for v in value]
        return struct


class OdooBlueprint(Blueprint):
    def __init__(self, model, common_filters=None, **kwargs):
        super().__init__(**kwargs)
        self._model = model
        self._common_filters = common_filters if common_filters else []

    def search(self, filters: list, fields=None, offset=None, limit=None, order=None, **options):
        filters = [self._common_filters + f for f in filters]
        return self.current_app.search(self._model, filters, fields, offset, limit, order, **options)

    def create(self, data, dry=False):
        return self.current_app.create(self._model, data, dry=dry)

    def write(self, data, dry=False):
        return self.current_app.write(self._model, data, dry=dry)

    def delete(self, _id, dry=False):
        return self.current_app.delete(self._model, _id, dry=dry)


class UserBlueprint(OdooBlueprint):

    def __init__(self, import_name='user', **kwargs):
        super().__init__("res.users", import_name=import_name, **kwargs)


class PartnerBlueprint(OdooBlueprint):

    def __init__(self, import_name='partner', **kwargs):
        super().__init__("res.partner", import_name=import_name, **kwargs)


class CustomerBlueprint(PartnerBlueprint):

    def __init__(self, import_name='customer', **kwargs):
        super().__init__(common_filters=[('customer', '=', True)], import_name=import_name, **kwargs)


class SupplierBlueprint(PartnerBlueprint):

    def __init__(self, import_name='supplier', **kwargs):
        super().__init__(common_filters=[('supplier', '=', True)], import_name=import_name, **kwargs)


class ProductBlueprint(OdooBlueprint):

    def __init__(self, import_name='product', **kwargs):
        super().__init__("product.product", import_name=import_name, **kwargs)
