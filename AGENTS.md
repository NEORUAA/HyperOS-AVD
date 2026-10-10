# Maintenance

- Reply in Chinese; write code, comments, and conventional commit messages in English.
- Share OS4 runtime fixes through Core and Apps. Select audited rules by content, ABI, and hardware capability, never by AVD names, ports, host workspace locations, or OTA filenames.
- Preserve hashes, provenance, ownership, frozen migration history, lifecycle flags, and user choices. Retain unknown/customized content with clear diagnostics.
- Keep one source of patch constants. Rebuild affected catalogs, assets, and receipts together; consumers must work offline without NDK or donor caches.
- Keep first-boot/PackageManager, Java/SELinux/kernel, and host prerequisites in their required layer; userdata modules do not survive factory reset.
- Preserve userdata, stable identity, unrelated modules, and loaded inodes. Do not wipe, weaken SELinux, or restart shared ADB to simplify a fix.
- Run meaningful affected tests and verify shared changes on Phone and Pad. Check actual loaded code and functions; distinguish confirmed fixes from skipped or unconfirmed cases.
- Measure before optimizing; keep resources adjustable and respect real CPU topology.
- Commit by concern. Push, publication, and release require explicit user requests.
- Minimize downloads/disk use; clean only verified task-owned disposable artifacts. Exclude private logs, identifiers, and userdata from Git/releases.
