"""The Logfather application package.

- core/  pure logic and models: no Qt, no network
- data/  Elastic access, caches, on-disk stores (QtCore signals allowed,
         no GUI imports)
- ui/    everything Qt: windows, widgets, dialogs, worker plumbing
"""
