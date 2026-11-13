import { PageHeader } from "@/components/ui";

import { TestCall } from "@/components/test-call";

export const metadata = { title: "Test call · Voice AI Receptionist" };

export default function TestCallPage() {
  return (
    <>
      <PageHeader
        title="Test call"
        subtitle="Talk to your receptionist in the browser, exactly as a caller would. Bookings you make are real, so cancel them after."
      />
      <TestCall />
    </>
  );
}
