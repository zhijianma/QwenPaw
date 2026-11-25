import { PageHeader } from "@/components/PageHeader";

import {
  useMarketplaceHeaderProps,
  type MarketplaceHeaderProps,
} from "./useMarketplaceHeaderProps";

export function MarketplaceHeader(props: MarketplaceHeaderProps) {
  return <PageHeader {...useMarketplaceHeaderProps(props)} />;
}
