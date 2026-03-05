# Adding Dependencies to the Decky Plugin

The Decky plugin runs on SteamOS, which has a minimal Python environment. Most third-party packages must be **bundled** into the plugin's `py_modules/` directory.

## Currently Bundled Dependencies

- **requests** — HTTP library
- **watchdog** — File system monitoring
- **PIL (Pillow)** — Image processing
- **certifi, charset_normalizer, idna, urllib3** — Transitive dependencies of requests

## How to Add a New Dependency

1. **Install the package into `py_modules/`:**
   ```bash
   cd decky_plugin
   pip3 install --target=py_modules --no-deps <package-name>
   ```

2. **Add the package to `.gitignore`** (following the existing pattern):
   ```
   py_modules/<package-name>/
   py_modules/<package-name>-*.dist-info/
   ```

3. **Update `DEPLOYMENT.md`** to list the new dependency in the "Required files in the ZIP" table.

4. **Test the build:**
   ```bash
   cd decky_plugin
   ./decky-build.sh
   ```

## Notes

- Use `--no-deps` to avoid installing transitive dependencies that may already be bundled
- If you need transitive dependencies, install them separately with `--no-deps`
- The `decky-build.sh` script automatically includes all `py_modules/` contents in the ZIP
- Bundled dependencies are excluded from git but included in the deployment ZIP

## Example: Adding Pillow

```bash
cd decky_plugin
pip3 install --target=py_modules --no-deps Pillow
```

Then add to `.gitignore`:
```
py_modules/PIL/
py_modules/pillow-*.dist-info/
py_modules/pillow.libs/
```
