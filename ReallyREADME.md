# ReallyREADME — Photo Gallery Cheat-Sheet

---

## 1. Create a new gallery

```bash
~/ns/new-gallery.sh <name> "optional description"
```

**Example:**
```bash
~/ns/new-gallery.sh portraits "Studio portraits"
```

What happens:
- Prompts you to **set a vault password** (choose a strong one)
- Creates `~/ns/galleries/portraits/` with an encrypted vault
- Auto-assigns the next free port (8765, 8766, 8767 …)
- Prints your drop folder path: `C:\photodrop\portraits\`

> **Password reminder** — if you ever forget a gallery password, check the photo on your phone tagged **`recovery`** and **`gallery`**.

---

## 2. Start a gallery

```bash
~/start-gallery.sh <name>
```

**Example:**
```bash
~/start-gallery.sh noir
```

What happens:
1. Prompts for the vault password
2. Unlocks and mounts the encrypted vault
3. Starts the file monitor watching `C:\photodrop\noir\` (creates the folder if needed)
4. Starts the gallery server
5. Open **http://localhost:8765** in your browser

To run multiple galleries simultaneously (each has its own port):
```bash
~/start-gallery.sh noir        # http://localhost:8765
~/start-gallery.sh portraits   # http://localhost:8766
```

To stop: press **Ctrl+C** in the terminal — the vault locks automatically.

### Gallery Hub (optional)

Lists all galleries, shows running status, lets you open them:

```bash
~/ns/start-hub.sh
```
Open **http://localhost:8764**

---

## 3. Add photos

### Option A — Windows drop folder (automatic)

While the gallery is running, drop image files into:

```
C:\photodrop\<gallery-name>\
```

Files are picked up within ~60 seconds, moved into the encrypted vault, auto-renamed to `image-YYYY-MM-DD-HHMMSS.jpg`, and checked for duplicates.

### Option B — Copy directly from Linux

```bash
cp /path/to/image.jpg ~/ns/galleries/noir/vault/photos/
```

Picked up within ~30 seconds.

### Option C — From Windows Explorer

Navigate to:
```
\\wsl.localhost\Ubuntu\home\garyk\ns\galleries\noir\vault\photos\
```
and paste files there directly.

> **Important:** the `vault/photos/` folder only exists while the gallery is running (vault is unlocked). Don't try to add files when the server is stopped.

### Supported formats

`.jpg` `.jpeg` `.png` `.tif` `.tiff` `.webp` `.heic` `.gif` `.bmp`

RAW formats (`.orf`, `.cr2`, `.nef`, etc.) are **not** displayed — convert to TIFF or JPEG first.

---

## 4. Passwords & security

Each gallery has its own vault password set when the gallery was created.

- The password unlocks the vault on startup and locks it again on stop
- Zip exports (logout → save session) are AES-256 encrypted with the same password
- **There is no password recovery** — if you lose it, the data in that vault is unrecoverable

> **Reminder:** your passwords are photographed and stored on your phone.  
> Search your phone photos for tags: **`recovery`** **`gallery`**

---

## 5. Useful commands

| Task | Command |
|---|---|
| Create gallery | `~/ns/new-gallery.sh <name> "desc"` |
| Start gallery | `~/start-gallery.sh <name>` |
| Start hub | `~/ns/start-hub.sh` |
| List galleries | `ls ~/ns/galleries/` |
| Check what port a gallery uses | `cat ~/ns/galleries/<name>/gallery.json` |
| Manually check vault is mounted | `mountpoint ~/ns/galleries/<name>/vault` |
| Force unmount a stuck vault | `fusermount -u ~/ns/galleries/<name>/vault` |

---

---

# Appendix — gocryptfs Encrypted Vaults (standalone)

gocryptfs encrypts a directory at the file level. Each file is encrypted individually. You keep two directories: a **ciphertext** directory (always on disk) and a **mountpoint** (only exists when unlocked).

This is completely independent of the photo gallery — you can use it to encrypt any folder.

---

## Linux / WSL2

### Install

```bash
sudo apt install gocryptfs
```

### Create a new vault

```bash
mkdir -p ~/my-vault-cipher   # ciphertext directory (keep this)
mkdir -p ~/my-vault          # mountpoint (will be empty when locked)
gocryptfs -init ~/my-vault-cipher
# prompted to set a password
```

### Unlock (mount)

```bash
gocryptfs ~/my-vault-cipher ~/my-vault
# prompted for password
# files are now accessible at ~/my-vault/
```

### Use it

Read and write files normally at `~/my-vault/`. They are transparently encrypted in `~/my-vault-cipher/`.

### Lock (unmount)

```bash
fusermount -u ~/my-vault
# ~/my-vault/ is now empty — data only exists encrypted in ~/my-vault-cipher/
```

### Backup

Back up `~/my-vault-cipher/` — this is the encrypted data. The mountpoint (`~/my-vault/`) is always empty when locked and never needs backing up.

### Access from Windows

While the vault is mounted in WSL2, the plaintext files are accessible from Windows at:
```
\\wsl.localhost\Ubuntu\home\<user>\my-vault\
```

---

## Windows 11 — Cryptomator (gocryptfs equivalent)

gocryptfs does not run natively on Windows. **Cryptomator** is the closest equivalent — open source, free, same concept (encrypts file-by-file, mounts as a virtual drive).

### Install Cryptomator

Download from **cryptomator.org** — available as a standard Windows installer.

### Create a vault

1. Open Cryptomator → **Add Vault** → **Create New Vault**
2. Choose a location (e.g. `C:\Users\you\Documents\my-vault-cipher\`)
3. Set a password
4. Click **Create Vault**

### Unlock

1. Select the vault in Cryptomator → **Unlock**
2. Enter password
3. A virtual drive appears (e.g. `Z:\`) — use it like any other drive

### Lock

Click **Lock** in Cryptomator — the virtual drive disappears.

### Notes

- Cryptomator vaults and gocryptfs vaults are **not interchangeable** (different format)
- Cryptomator vaults stored on `C:\` are accessible from WSL2 at `/mnt/c/...` when unlocked
- For cross-platform use (same vault on Windows and WSL2), use Cryptomator on Windows — then access the mounted drive from WSL2 via `/mnt/z/` (or whatever drive letter)

---

## Choosing between gocryptfs and Cryptomator

| | gocryptfs | Cryptomator |
|---|---|---|
| Platform | Linux / WSL2 | Windows / Mac / Linux |
| GUI | No (terminal only) | Yes |
| Open source | Yes | Yes |
| Free | Yes | Yes (donate-ware) |
| Format | gocryptfs | Cryptomator |
| Cross-platform vault | No | Yes |
| Used by this gallery | **Yes** | No |
