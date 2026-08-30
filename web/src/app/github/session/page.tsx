import { Suspense } from "react";
import { Flex } from "@chakra-ui/react";
import { GitHubSessionViewer } from "@/components/GitHubSessionViewer";

export default function GitHubSessionPage() {
  return (
    <Suspense fallback={<Flex h="100dvh" />}>
      <GitHubSessionViewer />
    </Suspense>
  );
}
