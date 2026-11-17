/**
 * `/app/invoices` (ADR 0032).
 *
 * A 404 from the API is not an error here: production does not serve invoices yet, and the screen
 * says "not shown here yet" rather than "could not be loaded".
 */
import { redirect } from "next/navigation";

import { isNotFound, isSignedOut } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { type Invoice, readInvoices } from "../../../lib/reads";
import { InvoicesScreen } from "../../../screens/InvoicesScreen";

export default async function InvoicesPage() {
  const { t, language } = await requestI18n();
  let invoices: Invoice[] | null = null;
  let notOnline = false;
  try {
    invoices = await readInvoices();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    notOnline = isNotFound(error);
  }
  return <InvoicesScreen i18n={{ t, language }} invoices={invoices} notOnline={notOnline} />;
}
