import { FileQuestion } from "lucide-react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/primitives";
import { EmptyState } from "@/components/ui/states";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        icon={FileQuestion}
        title="That page does not exist"
        body="The link may be out of date, or the record may have been archived."
        action={
          <Button variant="primary" asChild>
            <Link to="/">Back to overview</Link>
          </Button>
        }
      />
    </>
  );
}
