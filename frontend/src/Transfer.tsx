import { useState } from "react";
import { Library } from "./Library";

export function Transfer({ capturing }: { capturing: boolean }) {
  const [uploadOpened, setUploadOpened] = useState(false);
  return (
    <>
      <div className="heading">
        <h1>Transfer</h1>
      </div>
      <section className="transfer-section" aria-labelledby="rsync-heading">
        <h2 id="rsync-heading">rsync</h2>
        <p>Run on your Mac after stopping capture.</p>
        <pre className="rsync-command" tabIndex={0} aria-label="rsync command">
          <code>{`rsync -r --whole-file --info=progress2,stats2 --human-readable --ignore-existing \\
  --exclude='*.preview.jpg' \\
  --exclude='*.thumb.jpg' \\
  --exclude='.*' \\
  --exclude='*.tmp' \\
  --exclude='*.pending.*' \\
  pi@192.168.1.14:/home/pi/cameradeck/media/ \\
  "/Volumes/WD APFS/Faros-OTA/boxed/"`}</code>
        </pre>
        <p>
          Copies originals and metadata; skips previews, thumbnails, internal
          files and temporary sidecars. Existing files are skipped, not
          repaired.
        </p>
      </section>
      <details
        className="transfer-section"
        onToggle={(e) => {
          if (e.currentTarget.open) setUploadOpened(true);
        }}
      >
        <summary>S3 upload</summary>
        {uploadOpened && <Library capturing={capturing} uploading />}
      </details>
    </>
  );
}
