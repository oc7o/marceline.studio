/**
 * `/app/originals/new` — "add an original" (issue #29).
 *
 * The server decides whether it is offered at all (`can_add_originals`), so someone without the
 * permission gets the sentence and never the form; the form itself is an island, and the
 * fingerprinting code loads only when they press start.
 */
import { AddOriginalScreen } from "../../../../components/AddOriginalScreen";
import { OpenUploads } from "../../../../components/OpenUploads";
import { Banner } from "../../../../components/ui";
import { requestI18n } from "../../../../lib/i18n";
import { optional } from "../../../../lib/pageReads";
import { readAccount, readUploads } from "../../../../lib/reads";
import { slice } from "../../../../lib/translate";

export default async function AddOriginalPage() {
  const { t, language, resource } = await requestI18n();
  const account = await optional(readAccount);
  if (account === null) return null;
  // A file sent earlier may still be being fingerprinted: the customer left this page and came
  // back, which the words invite them to do (ADR 0044 amendment 2026-09-18).
  const uploads = account.can_upload_media ? await optional(readUploads) : null;

  return (
    <div className="flex flex-col gap-4">
      <a href="/app" className="tap rounded-sm text-body-s text-brand-ink underline">
        {t("addOriginal.back")}
      </a>
      <h1 className="font-display text-display-m text-ink">{t("addOriginal.heading")}</h1>
      {account.organisation === null ? (
        <Banner tone="caution" role="status">
          {t("home.noOrganisation")}
        </Banner>
      ) : !account.can_add_originals ? (
        <Banner role="status">{t("addOriginal.unavailable")}</Banner>
      ) : (
        <>
          {/*
            The one honest thing to say before the form, and it is said to everybody (issue #117).
            Hashing a video happens in the customer's own browser, which means reading the whole
            file: slow on a phone, and beyond some mobile browsers entirely once the file is
            large. The audience now arrives on a phone, so this cannot wait until
            they have picked a file and watched it stall.

            **Server-rendered, always visible, and the same text on every device.** Not a note
            that appears "on a phone": deciding that would mean reading the User-Agent or a client
            hint, which is a request-time fingerprinting signal we do not take, and it would mean
            an island on this route to measure the viewport. A sentence a desktop customer reads
            and shrugs at costs nothing; a phone customer who is not told loses an hour.
          */}
          <Banner role="note">{t("addOriginal.onAPhone")}</Banner>
          <OpenUploads i18n={{ t, language }} uploads={uploads} />
          <AddOriginalScreen
            language={language}
            words={slice(resource, "addOriginal", "common")}
            canUploadMedia={account.can_upload_media}
          />
        </>
      )}
    </div>
  );
}
