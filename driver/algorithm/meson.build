# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun

focaltech_algo = shared_library('focaltech.0752.algo',
    'entry.cpp',
    install: true)

focaltech_algo_dep = declare_dependency(
    link_with: focaltech_algo,
    include_directories: include_directories('.'))
