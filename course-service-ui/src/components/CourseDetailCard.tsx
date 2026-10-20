import { useNavigate } from "react-router-dom";
import type { CourseDetails } from "../types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { InfoField } from "./common/InfoField";
import { Badge } from "@/components/ui/badge";

export interface CourseDetailCardProps {
  courseDetails: CourseDetails;
}

export const CourseDetailCard: React.FC<CourseDetailCardProps> = ({
  courseDetails,
}) => {
  const navigate = useNavigate();
  const hasProfiles =
    courseDetails.profiles && courseDetails.profiles.length > 0;

  const handleViewProfiles = () => {
    navigate(
      `/course/${courseDetails.course_id}/${courseDetails.term_id}/profiles`,
    );
  };

  return (
    <Accordion
      type="single"
      collapsible
      className="w-full mb-6 border rounded-lg bg-card"
    >
      <AccordionItem value="course-details" className="border-0">
        <AccordionTrigger className="px-6 hover:no-underline text-lg font-semibold">
          {courseDetails.course_name} - {courseDetails.term_id}
        </AccordionTrigger>
        <AccordionContent className="px-6 pb-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <InfoField label="Course ID" value={courseDetails.course_id} />
            <InfoField label="Term" value={courseDetails.term_id} />
            {courseDetails.description && (
              <InfoField
                label="Course Description"
                value={courseDetails.description}
                className="col-span-full"
              />
            )}

            <div className="col-span-full">
              <Accordion type="single" collapsible className="w-full">
                <AccordionItem
                  value="profiles"
                  className={cn(!hasProfiles && "opacity-50")}
                >
                  <div className="flex items-center justify-between gap-3">
                    <AccordionTrigger
                      disabled={!hasProfiles}
                      className={cn(
                        "flex-1 hover:no-underline py-2",
                        !hasProfiles && "cursor-not-allowed",
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-muted-foreground text-xs uppercase tracking-wide">
                          Profiles
                        </span>
                        <Badge variant="secondary" className="text-xs">
                          {courseDetails.profiles?.length || 0}
                        </Badge>
                      </div>
                    </AccordionTrigger>
                    {hasProfiles && (
                      <Button
                        size="sm"
                        onClick={handleViewProfiles}
                        className="mr-4"
                      >
                        View Details
                      </Button>
                    )}
                  </div>
                  <AccordionContent>
                    {hasProfiles && (
                      <div className="bg-muted/50 rounded-md border-l-4 border-primary p-4 space-y-2">
                        {courseDetails.profiles!.map((profile, index) => (
                          <div
                            key={index}
                            className={cn(
                              "py-1.5 text-foreground",
                              index !== courseDetails.profiles!.length - 1 &&
                                "border-b border-border",
                            )}
                          >
                            {profile}
                          </div>
                        ))}
                      </div>
                    )}
                  </AccordionContent>
                </AccordionItem>
              </Accordion>
            </div>
          </div>
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
};
