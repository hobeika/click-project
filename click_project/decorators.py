#!/usr/bin/env python
# -*- coding:utf-8 -*-

from __future__ import print_function, absolute_import

import functools
import types
import logging

import click
import six

from click_project.lib import get_tabulate_formats, ParameterType
from click_project.config import config,  merge_settings
from click_project.completion import startswith
from click_project.overloads import command, group, option, flag, argument
from click_project.flow import flowdepends  # NOQA: F401
from click_project.core import settings_stores

LOGGER = logging.getLogger(__name__)


def param_config(name, *args, **kwargs):
    typ = kwargs.pop("typ", object)
    kls = kwargs.pop("kls", option)

    class Conf(typ):
        pass
    init_callback = kwargs.get("callback")

    def _subcommand_config_callback(ctx, attr, value):
        if not hasattr(config, name):
            setattr(config, name, Conf())
        setattr(getattr(config, name), attr.name, value)
        if init_callback is not None:
            value = init_callback(ctx, attr, value)
            setattr(getattr(config, name), attr.name, value)
        return value

    kwargs["expose_value"] = kwargs.get("expose_value", False)
    kwargs["callback"] = _subcommand_config_callback

    def decorator(func):
        return kls(*args, **kwargs)(func)

    return decorator


def use_settings(settings_name, settings_cls, override=True, default_level='context'):
    def decorator(f):
        if settings_name not in settings_stores:
            settings_stores[settings_name] = settings_cls()
        settings_store = settings_stores[settings_name]
        settings_store.recipe = None

        def compute_settings(context=True):
            settings_store.all_settings = {
                "global/preset": config.global_context_settings.get(
                    settings_name,
                    {}),
                "local/preset": config.local_context_settings.get(
                    settings_name,
                    {}),
                "env": config.env_settings.get(settings_name, {}),
                "commandline": config.command_line_settings.get(settings_name, {}),
            }
            if context:
                settings_store.all_settings.update(
                    {
                        "global": config.global_profile.get_settings(settings_name),
                        "workgroup": config.workgroup_profile and config.workgroup_profile.get_settings(settings_name),
                        "local": config.local_profile and config.local_profile.get_settings(settings_name),
                    }
                )
                for recipe in config.all_recipes:
                    settings_store.all_settings[recipe.name] = recipe.get_settings(settings_name)
                if config.local_profile:
                    name = config.local_profile.name
                    settings_store.all_settings[name] = config.local_profile.get_settings(settings_name)
            for key, value in settings_store.all_settings.copy().items():
                if value is None:
                    del settings_store.all_settings[key]

        def setup_settings(ctx):
            if ctx is not None and hasattr(ctx, "click_project_level"):
                level = ctx.click_project_level
            else:
                level = default_level
            if ctx is not None and hasattr(ctx, "click_project_recipe"):
                recipe = ctx.click_project_recipe
            else:
                recipe = "main"
            if recipe == "main":
                recipe = None
            if level == "context":
                compute_settings(False)
                s1, s2 = merge_settings(config.iter_settings(
                    profiles_only=True,
                    with_recipes=True,
                    recipe_short_name=recipe,
                ))
                profile = config.local_profile or config.global_profile
                if recipe:
                    profile = profile.get_recipe(recipe)
                    for r in config.iter_recipes(recipe):
                        settings_store.all_settings[r.name] = r.get_settings(settings_name)
                else:
                    compute_settings(True)
                settings_store.readlevel = level
            else:
                compute_settings(False)
                profile = config.profiles_per_level[level]
                profile = profile.get_recipe(recipe) if recipe else profile
                settings_store.readlevel = profile.name
                settings_store.all_settings[profile.name] = profile.get_settings(settings_name)
                s1, s2 = merge_settings(config.load_settings_from_profile(
                    profile,
                    with_recipes=True,
                    recipe_short_name=recipe,
                ))
                for recipe in config.filter_enabled_recipes(profile.recipes):
                    settings_store.all_settings[recipe.name] = recipe.get_settings(settings_name)

            readonly = s1 if override else s2
            readonly = readonly.get(settings_name, {})

            settings_store.writelevel = profile.name
            settings_store.writelevelname = profile.friendly_name
            settings_store.profile = profile
            settings_store.readonly = readonly
            settings_store.writable = profile.get_settings(settings_name)
            settings_store.write = profile.write_settings

        def level_callback(ctx, attr, value):
            if value:
                ctx.click_project_level = value
                setup_settings(ctx)
            return value

        def recipe_callback(ctx, attr, value):
            if value is not None:
                ctx.click_project_recipe = value
                setup_settings(ctx)
            return value

        class RecipeType(ParameterType):
            @property
            def choices(self):
                return [
                    r.short_name
                    for r in config.all_recipes
                ] + ["main"]

            def complete(self, ctx, incomplete):
                return [
                    candidate
                    for candidate in self.choices
                    if startswith(candidate, incomplete)
                ]

        for level in config.levels:
            f = flag('--{}'.format(level), "level", flag_value=level, help="Consider only the {} level".format(level), callback=level_callback)(f)
        f = flag('--context', "level", flag_value="context", help="Guess the level", callback=level_callback)(f)
        f = option('--recipe', type=RecipeType(), callback=recipe_callback)(f)

        setup_settings(None)

        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            setattr(config, settings_name, settings_store)
            del kwargs["recipe"]
            del kwargs["level"]
            LOGGER.debug("Will use the settings at level {}".format(
                settings_store.readlevel
            ))
            return f(*args, **kwargs)
        wrapped.inherited_params = ["recipe", "level"]
        return wrapped
    return decorator


pass_context = click.pass_context


def deprecated(version=None, message=None):
    def deprecated_decorator(command):
        callback = command.callback

        @functools.wraps(callback)
        def run_deprecated_command(*args, **kwargs):
            msg = "'%s' is deprecated" % command.path.replace('.', ' ')
            if version:
                msg += " since version %s" % version
            if message:
                msg += ". " + message
            LOGGER.deprecated(msg)
            return callback(*args, **kwargs)

        command.short_help = (command.short_help or "") + " (deprecated)"
        command.callback = run_deprecated_command
        return command

    return deprecated_decorator


def table_format(func=None, default=None, config_name='config.table.format'):
    def decorator(func):
        # not sure why, but python can't access the default value with a closure in a statement of this kind
        #     default = default
        # so we have to use another name
        actual_default = config.get_settings('values').get(config_name, {}).get('value') or default or 'simple'
        opts = [
            option('--format', default=actual_default, help='Table format', type=get_tabulate_formats()),
        ]
        for opt in reversed(opts):
            func = opt(func)
        return func
    return decorator(func) if func else decorator


def table_fields(func=None, choices=(), default=None):
    def decorator(func):
        fields_type = click.Choice(choices) if choices else None
        opts = [
            option('--field', 'fields', multiple=True, type=fields_type, default=default,
                   help="Only display the following fields in the output")
        ]
        for opt in reversed(opts):
            func = opt(func)
        return func
    return decorator(func) if func else decorator


# don't export the modules, so we can safely import all the decorators
__all__ = ['get_tabulate_formats', 'startswith', 'settings_stores', 'option',
           'argument', 'param_config', 'pass_context', 'table_fields',
           'ParameterType', 'group', 'table_format', 'deprecated', 'flag',
           'merge_settings', 'command', 'use_settings']


if __name__ == '__main__':
    # generate the __all__ content for this file
    symbols = [k for k, v in six.iteritems(dict(globals())) if not isinstance(v, types.ModuleType)]
    symbols = [k for k in symbols if not k.startswith('_')]
    symbols = [k for k in symbols if k not in ['print_function', 'absolute_import']]
    print('__all__ = %s' % repr(symbols))
