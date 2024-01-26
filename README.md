# envreport: why doesn't it work anymore??

Prepare reports on environments and report the differences between two environments.

First and foremost, envreport produces a report on an environment in a diffable format.

Inspired by [pyenvdiff](https://github.com/jnmclarty/pyenvdiff-lib), which appears to have gone unmaintained.

## Goals

The main goal is to help folks answer the question "It worked here, it didn't work there. WHY? WHAT'S DIFFERENT?!"

Design goals:

1. collect information about an environment, so that it can be easily compared with another
2. be compatible, easily distributable (a single Python script, require only standard library for _relatively_ old Python)
3. be modular, so additional sources of information can be added, as needed
4. always produce _something_, even if data collection fails
