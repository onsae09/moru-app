/**
 * Pixel anvil logo (design SVG, verbatim). Standalone, import-free module
 * so gates rendered under `bun test` never drag the preload bridge in.
 */

export function MoruLogo({ width = 24, height = 20 }: { width?: number; height?: number }) {
  return (
    <svg
      viewBox="0 0 24 20"
      width={width}
      height={height}
      xmlns="http://www.w3.org/2000/svg"
      shapeRendering="crispEdges"
    >
      <rect className="text-accent" x="5" y="1" width="14" height="2" fill="currentColor" />
      <rect className="text-accent" x="0" y="3" width="24" height="4" fill="currentColor" />
      <rect className="text-accent" x="8" y="7" width="8" height="6" fill="currentColor" />
      <rect className="text-accent" x="5" y="13" width="14" height="2" fill="currentColor" />
      <rect className="text-accent" x="2" y="15" width="20" height="4" fill="currentColor" />
    </svg>
  );
}
