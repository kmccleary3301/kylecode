#ifndef FS_PROTO_FS_H
#define FS_PROTO_FS_H
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t version;
    uint32_t file_count;
    uint64_t data_offset;
} fs_header_t;

typedef struct {
    char name[64];
    uint64_t offset;
    uint64_t size;
    uint64_t mtime;
} fs_entry_t;

/* Initialize or open storage. Returns 0 on success. */
int fs_init(const char* storage_path);
int fs_close(void);

/* Write/overwrite a file at path with data of given size. */
int fs_write(const char* path, const void* data, size_t size);
int fs_read(const char* path, void* buffer, size_t max_size, size_t* out_size);
int fs_rename(const char* old_path, const char* new_path);
int fs_list(char*** out_names, size_t* out_count);

#endif
