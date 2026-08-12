# Changelog

## [0.1.4] - Unreleased

- Make the Three.js/web-ifc viewer and browser chat bundle an optional
  `ifc-console[viewer]` installation, with a viewer-free core wheel.
- Reorganize and simplify the documentation, with a shorter onboarding path,
  clearer safety guidance, grouped settings, and task-based navigation.
- Fail closed on Python 3.10 and 3.11 when complete generated-code isolation is
  requested, because those runtimes cannot audit raw thread creation; `auto`
  reports its guarded fallback and `strict` refuses it.

The changelog can be found on the
[GitHub Releases page](https://github.com/nbharathik/ifc-console/releases).
