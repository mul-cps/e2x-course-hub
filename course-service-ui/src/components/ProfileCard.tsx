import { InfoField } from "./common/InfoField";
import type { Profile } from "../types";
import { Card, CardContent, CardHeader } from "./ui/card";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "./ui/accordion";

export interface ProfileCardProps {
  profile: Profile;
}

export const ProfileCard: React.FC<ProfileCardProps> = ({ profile }) => {
  const hasEnvironment = Object.keys(profile.runtime.environment).length > 0;

  return (
    <Card className="mb-6">
      <CardHeader>
        <h3 className="m-0 text-xl font-semibold">{profile.display_name}</h3>
      </CardHeader>

      <CardContent className="space-y-4">
        <InfoField label="Profile Name:" value={profile.name} />

        <Accordion type="single" collapsible className="w-full">
          <AccordionItem value="runtime">
            <AccordionTrigger className="text-base font-semibold">
              Runtime Configuration
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-4">
                <div className="space-y-2">
                  <h4 className="font-semibold text-sm text-muted-foreground uppercase tracking-wide m-0">
                    Image
                  </h4>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <InfoField
                      label="Name:"
                      value={profile.runtime.image.name}
                    />
                    <InfoField label="Tag:" value={profile.runtime.image.tag} />
                    <InfoField
                      label="Pull Policy:"
                      value={profile.runtime.image.pullPolicy}
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <h4 className="font-semibold text-sm text-muted-foreground uppercase tracking-wide m-0">
                    Resources
                  </h4>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <InfoField
                      label="CPU Guarantee:"
                      value={profile.runtime.resources.cpu_guarantee}
                    />
                    <InfoField
                      label="CPU Limit:"
                      value={profile.runtime.resources.cpu_limit}
                    />
                    <InfoField
                      label="Memory Guarantee:"
                      value={profile.runtime.resources.mem_guarantee}
                    />
                    <InfoField
                      label="Memory Limit:"
                      value={profile.runtime.resources.mem_limit}
                    />
                  </div>
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>

          {hasEnvironment && (
            <AccordionItem value="environment">
              <AccordionTrigger className="text-base font-semibold">
                Environment Variables (
                {Object.keys(profile.runtime.environment).length})
              </AccordionTrigger>
              <AccordionContent>
                <div className="space-y-2 p-3 bg-muted/50 rounded border-l-4 border-blue-500">
                  {Object.entries(profile.runtime.environment).map(
                    ([key, value]) => (
                      <div key={key} className="flex gap-2 items-baseline">
                        <span className="font-mono text-sm font-semibold text-foreground min-w-0 break-all">
                          {key}:
                        </span>
                        <span className="font-mono text-sm text-muted-foreground break-all flex-1">
                          {String(value)}
                        </span>
                      </div>
                    ),
                  )}
                </div>
              </AccordionContent>
            </AccordionItem>
          )}
        </Accordion>
      </CardContent>
    </Card>
  );
};
