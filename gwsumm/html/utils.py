# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2013)
#
# This file is part of GWSumm.
#
# GWSumm is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWSumm is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWSumm.  If not, see <http://www.gnu.org/licenses/>.

"""Utilties for HTML generation
"""

import subprocess

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'


def highlight_syntax(filepath, format_):
    """Return an HTML-formatted copy of the file with syntax highlighting
    """
    highlight = ['highlight', '--out-format', 'html', '--syntax', format_,
                 '--inline-css', '--fragment', '--input', filepath]
    try:
        process = subprocess.Popen(highlight, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError:
        with open(filepath, 'r') as fobj:
            return fobj.read()
    else:
        out, err = process.communicate()
        if process.returncode != 0:
            with open(filepath, 'r') as fobj:
                return fobj.read()
        else:
            return out