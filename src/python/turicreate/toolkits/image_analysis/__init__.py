# -*- coding: utf-8 -*-
# Copyright © 2017 Apple Inc. All rights reserved.
#
# Use of this source code is governed by a BSD-3-clause license that can
# be found in the LICENSE.txt file or at https://opensource.org/licenses/BSD-3-Clause
from __future__ import print_function as _
from __future__ import division as _
from __future__ import absolute_import as _

__all__ = ['image_analysis']

from turicreate._deps import LazyModuleLoader as _LazyModuleLoader

_mod_par = 'turicreate.toolkits.image_analysis.'
image_analysis = _LazyModuleLoader(_mod_par + 'image_analysis')
