import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import { queryKeys } from "./queryKeys";

export { queryKeys };

export function useMe() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: () => unwrap(api.GET("/api/v1/auth/me")),
    staleTime: 5 * 60_000,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { username: string; password: string }) =>
      unwrap(api.POST("/api/v1/auth/login", { body })),
    onSuccess: (me) => {
      queryClient.clear();
      queryClient.setQueryData(queryKeys.me, me);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/auth/logout")),
    onSuccess: (me) => {
      queryClient.clear();
      queryClient.setQueryData(queryKeys.me, me);
    },
  });
}

export function useCounts() {
  return useQuery({
    queryKey: queryKeys.counts,
    queryFn: () => unwrap(api.GET("/api/v1/review/counts")),
    refetchInterval: 60_000,
  });
}

export function useReferenceSummaries() {
  return useQuery({
    queryKey: queryKeys.referenceSummaries,
    queryFn: () => unwrap(api.GET("/api/v1/review/references")),
  });
}
