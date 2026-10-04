# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-only
"""Solve YouTube's URL challenge with Apple's on-device JavaScriptCore."""

import ctypes
import json
from os import path


def _runtime():
    lib = ctypes.CDLL('/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore')
    pointer = ctypes.c_void_p
    signatures = {
        'JSGlobalContextCreate': (pointer, [pointer]),
        'JSGlobalContextRelease': (None, [pointer]),
        'JSStringCreateWithUTF8CString': (pointer, [ctypes.c_char_p]),
        'JSStringRelease': (None, [pointer]),
        'JSStringGetMaximumUTF8CStringSize': (ctypes.c_size_t, [pointer]),
        'JSStringGetUTF8CString': (ctypes.c_size_t, [pointer, pointer, ctypes.c_size_t]),
        'JSEvaluateScript': (pointer, [pointer, pointer, pointer, pointer, ctypes.c_int,
                                      ctypes.POINTER(pointer)]),
        'JSValueToStringCopy': (pointer, [pointer, pointer, ctypes.POINTER(pointer)]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype = result
        function.argtypes = arguments
    return lib


def available():
    try:
        _runtime()
        return True
    except (OSError, AttributeError):
        return False


class CalculateN(object):
    def __init__(self, player):
        self._context = None
        self._lib = _runtime()
        self._context = self._lib.JSGlobalContextCreate(None)
        if not self._context:
            raise ValueError('Unable to create JavaScript context')
        self._player = player
        self._preprocessed = None
        self._cache = {}
        try:
            for name in ('lib.min.js', 'core.min.js'):
                with open(path.join(path.dirname(__file__), 'vendor', name), encoding='utf-8') as source:
                    self._evaluate(source.read())
                if name == 'lib.min.js':
                    self._evaluate('Object.assign(globalThis, lib);')
        except Exception:
            self.close()
            raise

    def close(self):
        if self._context:
            self._lib.JSGlobalContextRelease(self._context)
            self._context = None

    def __del__(self):
        self.close()

    def _evaluate(self, script):
        lib = self._lib
        string = lib.JSStringCreateWithUTF8CString(script.encode('utf-8'))
        exception = ctypes.c_void_p()
        try:
            result = lib.JSEvaluateScript(self._context, string, None, None, 1,
                                          ctypes.byref(exception))
            if exception.value or not result:
                raise ValueError('JavaScript challenge evaluation failed')
            text = lib.JSValueToStringCopy(self._context, result, ctypes.byref(exception))
            if exception.value or not text:
                raise ValueError('JavaScript challenge returned no value')
            try:
                buffer = ctypes.create_string_buffer(lib.JSStringGetMaximumUTF8CStringSize(text))
                lib.JSStringGetUTF8CString(text, buffer, len(buffer))
                return buffer.value.decode('utf-8')
            finally:
                lib.JSStringRelease(text)
        finally:
            lib.JSStringRelease(string)

    def calculate_n(self, challenge):
        if challenge in self._cache:
            return self._cache[challenge]
        request = {'requests': [{'type': 'n', 'challenges': [challenge]}]}
        if self._preprocessed is None:
            request.update(type='player', player=self._player, output_preprocessed=True)
        else:
            request.update(type='preprocessed', preprocessed_player=self._preprocessed)
        result = json.loads(self._evaluate('JSON.stringify(jsc(%s))' % json.dumps(request)))
        if result.get('type') == 'error':
            raise ValueError('Unable to prepare YouTube URL challenge')
        response = result['responses'][0]
        if response.get('type') == 'error':
            raise ValueError('Unable to solve YouTube URL challenge')
        self._preprocessed = result.get('preprocessed_player', self._preprocessed)
        solution = response['data'][challenge]
        if not solution or solution == challenge or solution.startswith('enhanced_except_'):
            raise ValueError('Invalid YouTube URL challenge result')
        self._cache[challenge] = solution
        return solution
