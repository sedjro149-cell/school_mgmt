# notifications/utils.py
import logging
from django.template import Template, Context
from django.template.exceptions import TemplateSyntaxError

logger = logging.getLogger(__name__)

def render_django_template(template_str: str, payload: dict) -> str:
    """
    Render a Django-style template string using django.template.Template.
    Returns a safe fallback on error and logs the exception.
    """
    if not template_str:
        return ''
    try:
        ctx = Context(payload or {})
        tpl = Template(template_str)
        return tpl.render(ctx)
    except (TemplateSyntaxError, Exception) as e:
        logger.exception("Django template render error: %s | tpl: %s | payload: %s", e, template_str, payload)
        # safe readable fallback (avoid exposing stack traces)
        return (template_str if len(template_str) < 200 else template_str[:200] + '...')
